import React, {useEffect, useMemo, useState} from 'react';

const formatJson = value => JSON.stringify(value ?? {}, null, 2);
const emptyContextOptions = () => ({
  sampleRows: false,
  statistics: false,
});
const PAGE_LIMIT = 100;
const MAX_GENERATION_FIELDS = 25;

const pagePath = (path, cursor = null) => (
  `${path}?limit=${PAGE_LIMIT}${cursor
    ? `&cursor=${encodeURIComponent(cursor)}`
    : ''}`
);

const nextCursor = result => (
  typeof result?.pagination?.nextCursor === 'string'
    ? result.pagination.nextCursor
    : null
);

function generationMatchesContext(generation, contextOptions) {
  const returnedOptions = generation?.contextOptions;
  return (
    returnedOptions
    && Object.keys(returnedOptions).length === 2
    && returnedOptions.sampleRows === contextOptions.sampleRows
    && returnedOptions.statistics === contextOptions.statistics
    && generation.metadataOnly === !(
      contextOptions.sampleRows || contextOptions.statistics
    )
  );
}

export function semanticProposalPayload(
  asset,
  text,
  explanation = '',
  operations = null,
) {
  if (!asset || !Number.isInteger(asset.version)) {
    throw new Error('Select a versioned semantic asset before preparing a proposal.');
  }
  let proposalOperations = operations;
  if (proposalOperations === null) {
    let curated;
    try {
      curated = JSON.parse(text);
    } catch (error) {
      throw new Error(`Curated metadata must be valid JSON: ${error.message}`);
    }
    if (!curated || typeof curated !== 'object' || Array.isArray(curated)) {
      throw new Error('Curated metadata must be a JSON object.');
    }
    proposalOperations = [{op: 'set', path: '/curated', value: curated}];
  } else if (!Array.isArray(proposalOperations) || proposalOperations.length === 0) {
    throw new Error('Generated semantic operations are unavailable.');
  }
  const payload = {
    assetId: asset.id,
    baseVersion: asset.version,
    operations: proposalOperations,
  };
  const reason = explanation.trim();
  if (reason) payload.explanation = reason;
  return payload;
}

function assetName(asset) {
  return (
    asset?.curated?.displayName
    || asset?.generated?.displayName
    || asset?.generated?.qualifiedName
    || asset?.generated?.name
    || asset?.id
  );
}

function proposalState(proposal) {
  return proposal?.state || proposal?.status || 'unknown';
}

function stableFields(asset) {
  return (asset?.generated?.fields || []).filter(field => (
    typeof field?.id === 'string' && field.id.trim()
  ));
}

export function hasGenerationPermission(identity) {
  const scopes = Array.isArray(identity?.scopes) ? identity.scopes : [];
  return (
    identity?.actor === 'admin'
    || scopes.includes('full')
    || (
      scopes.includes('semantic:inspect')
      && scopes.includes('semantic:generate')
    )
  );
}

export function hasGenerationDataPermission(identity) {
  const scopes = Array.isArray(identity?.scopes) ? identity.scopes : [];
  return (
    identity?.actor === 'admin'
    || scopes.includes('full')
    || scopes.includes('semantic:data')
  );
}

export function hasSourcePermission(identity) {
  const scopes = Array.isArray(identity?.scopes) ? identity.scopes : [];
  return (
    identity?.actor === 'admin'
    || scopes.includes('full')
    || (
      scopes.includes('semantic:inspect')
      && scopes.includes('semantic:source')
    )
  );
}

function hasArchivePermission(identity) {
  const scopes = Array.isArray(identity?.scopes) ? identity.scopes : [];
  return identity?.actor === 'admin' || scopes.includes('full') || (
    scopes.includes('semantic:inspect') && scopes.includes('semantic:admin')
  );
}

export function SemanticCatalog({api, close, identity}) {
  const [service, setService] = useState(null);
  const [assets, setAssets] = useState([]);
  const [catalogCursor, setCatalogCursor] = useState(null);
  const [sourceRelations, setSourceRelations] = useState([]);
  const [sourceCursor, setSourceCursor] = useState(null);
  const [sourceId, setSourceId] = useState('');
  const [derivedProfiles, setDerivedProfiles] = useState([]);
  const [derivedCursor, setDerivedCursor] = useState(null);
  const [deliveryBlockers, setDeliveryBlockers] = useState([]);
  const [deliveryBlockersMore, setDeliveryBlockersMore] = useState(false);
  const [proposals, setProposals] = useState([]);
  const [proposalCursor, setProposalCursor] = useState(null);
  const [selectedId, setSelectedId] = useState('');
  const [curatedText, setCuratedText] = useState('{}');
  const [explanation, setExplanation] = useState('');
  const [search, setSearch] = useState('');
  const [review, setReview] = useState(null);
  const [proposalReview, setProposalReview] = useState(null);
  const [history, setHistory] = useState(null);
  const [historyCursor, setHistoryCursor] = useState(null);
  const [busy, setBusy] = useState(false);
  const [generating, setGenerating] = useState('');
  const [generationProgress, setGenerationProgress] = useState(null);
  const [generationMode, setGenerationMode] = useState('');
  const [generationFieldIds, setGenerationFieldIds] = useState([]);
  const [contextOptions, setContextOptions] = useState(emptyContextOptions);
  const [generatedDraft, setGeneratedDraft] = useState(null);
  const [createdProposal, setCreatedProposal] = useState(null);
  const [advanced, setAdvanced] = useState(false);
  const [wizardStep, setWizardStep] = useState(1);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  const selected = assets.find(asset => asset.id === selectedId);
  const fields = stableFields(selected);
  const generationCapability = service?.capabilities?.generation;
  const generationAvailable = (
    generationCapability?.available === true
    && generationCapability.provider === 'gemini'
    && generationCapability.metadataOnly === true
  );
  const generationTargets = Array.isArray(generationCapability?.targets)
    ? generationCapability.targets
    : [];
  const sampleRowsCapability = generationCapability?.contextOptions?.sampleRows;
  const statisticsCapability = generationCapability?.contextOptions?.statistics;
  const generationPermitted = hasGenerationPermission(identity);
  const generationDataPermitted = hasGenerationDataPermission(identity);
  const sourcePermitted = hasSourcePermission(identity);
  const archivePermitted = hasArchivePermission(identity);
  const generationRunning = !!generating;
  const visibleAssets = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return assets;
    return assets.filter(asset => (
      `${asset.id} ${assetName(asset)} ${formatJson(asset.generated)} ${formatJson(asset.curated)}`
        .toLowerCase()
        .includes(needle)
    ));
  }, [assets, search]);

  const load = async (preferredId = selectedId) => {
    const [
      statusResult,
      catalogResult,
      sourceResult,
      derivedResult,
      proposalResult,
    ] = await Promise.all([
      api('/api/semantic/status'),
      api(pagePath('/api/semantic/catalog')),
      sourcePermitted
        ? api(pagePath('/api/semantic/source/relations'))
        : Promise.resolve({relations: []}),
      api(pagePath('/api/semantic/derived-profiles')),
      api(pagePath('/api/semantic/proposals')),
    ]);
    const nextAssets = catalogResult.assets || [];
    const nextSources = sourceResult.relations || [];
    setService(statusResult);
    setAssets(nextAssets);
    setCatalogCursor(nextCursor(catalogResult));
    setSourceRelations(nextSources);
    setSourceCursor(nextCursor(sourceResult));
    setSourceId(current => (
      nextSources.some(source => source.assetId === current)
        ? current
        : nextSources[0]?.assetId || ''
    ));
    setDerivedProfiles(derivedResult.derivedProfiles || []);
    setDerivedCursor(nextCursor(derivedResult));
    setDeliveryBlockers(derivedResult.deliveryBlockers || []);
    setDeliveryBlockersMore(derivedResult.deliveryBlockersMore === true);
    setProposals(proposalResult.proposals || []);
    setProposalCursor(nextCursor(proposalResult));
    setProposalReview(null);
    setHistory(null);
    setHistoryCursor(null);
    setGeneratedDraft(null);
    setCreatedProposal(null);
    const nextId = nextAssets.some(asset => asset.id === preferredId)
      ? preferredId
      : nextAssets[0]?.id || '';
    setSelectedId(nextId);
    const next = nextAssets.find(asset => asset.id === nextId);
    setGenerationFieldIds([]);
    setGenerationMode('');
    setContextOptions(emptyContextOptions());
    setCuratedText('');
  };

  useEffect(() => {
    let active = true;
    setBusy(true);
    load()
      .catch(reason => active && setError(reason.message))
      .finally(() => active && setBusy(false));
    return () => {
      active = false;
    };
  }, []);

  const chooseAsset = id => {
    const next = assets.find(asset => asset.id === id);
    setSelectedId(id);
    setCuratedText('');
    setReview(null);
    setProposalReview(null);
    setHistory(null);
    setHistoryCursor(null);
    setGeneratedDraft(null);
    setCreatedProposal(null);
    setGenerationFieldIds([]);
    setGenerationMode('');
    setContextOptions(emptyContextOptions());
    setExplanation('');
    setError('');
    setNotice('');
    setWizardStep(2);
  };

  const switchMode = () => {
    setAdvanced(value => !value);
    setCuratedText('');
    setExplanation('');
    setGenerationFieldIds([]);
    setGenerationMode('');
    setContextOptions(emptyContextOptions());
    setGeneratedDraft(null);
    setCreatedProposal(null);
    setReview(null);
    setProposalReview(null);
    setWizardStep(1);
    setNotice('');
  };

  const prepare = async () => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const payload = semanticProposalPayload(
        selected,
        curatedText,
        explanation,
        generatedDraft?.operations || null,
      );
      const result = await api('/api/semantic/proposals/check', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      setReview(result.check);
      setNotice('Proposal checked. Review the focused diff before creating it.');
    } catch (reason) {
      setReview(null);
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  };

  const createProposal = async () => {
    setBusy(true);
    setError('');
    try {
      const payload = semanticProposalPayload(
        selected,
        curatedText,
        explanation,
        generatedDraft?.operations || null,
      );
      const result = await api('/api/semantic/proposals', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...payload, fingerprint: review.fingerprint}),
      });
      const proposal = result?.proposal;
      if (!proposal?.id) {
        throw new Error('The created semantic proposal was not returned.');
      }
      setReview(null);
      await load(selected.id);
      const storedResult = await api(
        `/api/semantic/proposals/${encodeURIComponent(proposal.id)}`,
      );
      const stored = storedResult?.proposal;
      if (
        !stored
        || stored.id !== proposal.id
        || proposalState(stored) !== 'pending'
        || !Array.isArray(stored.diff)
      ) {
        throw new Error('The stored proposal is unavailable for final review.');
      }
      setCreatedProposal(stored);
      setProposalReview(stored);
      setWizardStep(4);
      setNotice(`Created proposal ${proposal.id}. Review it once more, then apply it.`);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  };

  const archiveSelected = async () => {
    if (!selected || !window.confirm(`Archive semantic profile ${selected.id}? The database layer will not be changed.`)) return;
    setBusy(true);
    setError('');
    try {
      await api(`/api/semantic/catalog/objects/${encodeURIComponent(selected.id)}/archive`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirmed: true}),
      });
      setNotice(`Archived semantic profile ${selected.id}.`);
      await load();
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  };

  const decide = async (proposal, action) => {
    if (action === 'apply') {
      if (
        proposalReview?.id !== proposal.id
        || proposalState(proposalReview) !== 'pending'
      ) {
        setError('Review the stored proposal explanation and focused diff before applying it.');
        return;
      }
      if (!window.confirm(`Apply reviewed semantic proposal ${proposal.id}?`)) {
        return;
      }
    }
    setBusy(true);
    setError('');
    try {
      await api(`/api/semantic/proposals/${encodeURIComponent(proposal.id)}/${action}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(
          action === 'decline'
            ? {confirmed: true, reason: 'Declined in dashboard'}
            : {confirmed: true},
        ),
      });
      setNotice(action === 'apply'
        ? `Applied proposal ${proposal.id}. Choose a layer to start a new semantic task.`
        : `Declined proposal ${proposal.id}.`);
      if (action === 'apply') {
        setCreatedProposal(null);
        setWizardStep(1);
      }
      await load(selectedId);
      if (action === 'apply' && !advanced) {
        setSelectedId('');
        setSearch('');
      }
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  };

  const reviewProposal = async proposal => {
    setBusy(true);
    setError('');
    setNotice('');
    setProposalReview(null);
    try {
      const result = await api(
        `/api/semantic/proposals/${encodeURIComponent(proposal.id)}`,
      );
      const stored = result.proposal;
      if (
        !stored
        || stored.id !== proposal.id
        || proposalState(stored) !== 'pending'
        || !Array.isArray(stored.diff)
      ) {
        throw new Error('The stored pending proposal evidence is unavailable.');
      }
      setProposalReview(stored);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  };

  const repair = async profile => {
    if (!window.confirm(`Retry semantic delivery for derived_layers.${profile.name}?`)) {
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api(
        `/api/semantic/derived-profiles/${encodeURIComponent(profile.name)}/repair`,
        {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({confirmed: true}),
        },
      );
      setNotice(`Semantic delivery retry queued for derived_layers.${profile.name}.`);
      await load(selectedId);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  };

  const loadHistory = async () => {
    if (!selected) return;
    setBusy(true);
    setError('');
    try {
      const result = await api(
        pagePath(
          `/api/semantic/catalog/objects/${encodeURIComponent(selected.id)}/history`,
        ),
      );
      if (!Array.isArray(result.history)) {
        throw new Error('Semantic asset history is unavailable.');
      }
      setHistory(result.history);
      setHistoryCursor(nextCursor(result));
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  };

  const loadMore = async ({
    path,
    cursor,
    field,
    setItems,
    setCursor,
    afterLoad,
  }) => {
    if (!cursor) return;
    setBusy(true);
    setError('');
    try {
      const result = await api(pagePath(path, cursor));
      const items = result?.[field];
      if (!Array.isArray(items)) {
        throw new Error(`Semantic ${field} page is unavailable.`);
      }
      setItems(current => [...current, ...items]);
      setCursor(nextCursor(result));
      if (afterLoad) afterLoad(result);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  };

  const syncSource = async () => {
    const source = sourceRelations.find(item => item.assetId === sourceId);
    if (!source) return;
    if (!window.confirm(
      `Synchronize metadata for ${source.alias}:${source.schema}.${source.relation}?`,
    )) {
      return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await api('/api/semantic/source/sync', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          alias: source.alias,
          schema: source.schema,
          relation: source.relation,
        }),
      });
      if (
        !result?.asset
        || result.asset.id !== source.assetId
        || !['register', 'refresh', 'unchanged'].includes(result.operation)
      ) {
        throw new Error('Semantic source synchronization returned invalid evidence.');
      }
      setNotice(
        `${result.operation === 'register'
          ? 'Registered'
          : result.operation === 'refresh'
            ? 'Refreshed'
            : 'Already current'} `
        + `${source.schema}.${source.relation} from metadata only.`,
      );
      await load(result.asset.id);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  };

  const generateDraft = async target => {
    if (!selected || !generationAvailable || !generationPermitted) return;
    if (
      target.kind === 'field'
      && !fields.some(field => field.id === target.fieldId)
    ) {
      setError('Select a field with a stable semantic ID.');
      return;
    }
    setGenerating(target.kind);
    setError('');
    setNotice('');
    setReview(null);
    setGeneratedDraft(null);
    const requestedContext = {...contextOptions};
    try {
      const result = await api('/api/semantic/generate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          assetId: selected.id,
          target,
          contextOptions: requestedContext,
        }),
      });
      const draft = result?.draft;
      const generation = result?.generation;
      if (
        !draft
        || draft.assetId !== selected.id
        || draft.baseVersion !== selected.version
        || draft.target?.kind !== target.kind
        || (
          target.kind === 'field'
          && draft.target?.fieldId !== target.fieldId
        )
        || !Array.isArray(draft.operations)
        || draft.operations.length === 0
        || generation?.provider !== 'gemini'
        || !generationMatchesContext(generation, requestedContext)
        || generation.proposalCreated !== false
      ) {
        throw new Error('Gemini returned an invalid semantic proposal draft.');
      }
      setGeneratedDraft({
        operations: draft.operations,
        target: draft.target,
        model: generation.model,
      });
      if (typeof draft.explanation === 'string' && draft.explanation.trim()) {
        setExplanation(draft.explanation);
      }
      setNotice(
        'Gemini draft loaded for review. Check it before creating any proposal.',
      );
      setWizardStep(3);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setGenerating('');
    }
  };

  const generateFieldDrafts = async () => {
    if (!selected || !generationAvailable || !generationPermitted) return;
    if (
      generationFieldIds.length === 0
      || generationFieldIds.length > MAX_GENERATION_FIELDS
    ) {
      setError(`Select between one and ${MAX_GENERATION_FIELDS} fields with stable semantic IDs.`);
      return;
    }
    setGenerating('field');
    setGenerationProgress({current: 0, total: generationFieldIds.length});
    setError('');
    setNotice('');
    setReview(null);
    setGeneratedDraft(null);
    const requestedContext = {...contextOptions};
    try {
      let completed = 0;
      const settled = await Promise.allSettled(generationFieldIds.map(async fieldId => {
        const target = {kind: 'field', fieldId};
        try {
          const result = await api('/api/semantic/generate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              assetId: selected.id,
              target,
              contextOptions: requestedContext,
            }),
          });
          const draft = result?.draft;
          const generation = result?.generation;
          if (
            !draft
            || draft.assetId !== selected.id
            || draft.baseVersion !== selected.version
            || draft.target?.kind !== 'field'
            || draft.target?.fieldId !== fieldId
            || !Array.isArray(draft.operations)
            || draft.operations.length === 0
            || generation?.provider !== 'gemini'
            || !generationMatchesContext(generation, requestedContext)
            || generation.proposalCreated !== false
          ) {
            throw new Error('Gemini returned an invalid semantic proposal draft.');
          }
          return {draft, generation};
        } finally {
          completed += 1;
          setGenerationProgress({
            current: completed,
            total: generationFieldIds.length,
          });
        }
      }));
      const failure = settled.find(result => result.status === 'rejected');
      if (failure) {
        throw failure.reason;
      }
      const generated = settled.map(result => result.value);
      setGeneratedDraft({
        operations: generated.flatMap(item => item.draft.operations),
        target: {kind: 'fields', fieldIds: generationFieldIds},
        model: generated[0].generation.model,
      });
      const selectedContext = [
        requestedContext.sampleRows && '5% capped row samples',
        requestedContext.statistics && 'table/column statistics',
      ].filter(Boolean).join(' and ');
      setExplanation(
        (selectedContext
          ? `Gemini drafts for ${generationFieldIds.length} fields using explicitly selected bounded context: ${selectedContext}. `
          : `Gemini metadata-only drafts for ${generationFieldIds.length} fields. `)
        + 'Review every generated value before checking or creating a semantic proposal.',
      );
      setNotice(
        'Gemini field drafts loaded for review. Check them before creating any proposal.',
      );
      setWizardStep(3);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setGenerating('');
      setGenerationProgress(null);
    }
  };

  return <div className="modal-backdrop">
    <section className="panel security-panel semantic-panel">
      <div className="form-head">
        <div>
          <h2>Semantic catalog</h2>
          <p>Choose a profile, generate a reviewed draft, then create a proposal.</p>
        </div>
        <div className="semantic-actions">
          <button className="icon" disabled={generationRunning} onClick={switchMode}>
            {advanced ? 'Guided mode' : 'Advanced mode'}
          </button>
          <button disabled={generationRunning} onClick={close}>Close</button>
        </div>
      </div>
      {advanced && service && <details className="semantic-catalog-info">
        <summary>Catalog information</summary>
        <p className="muted">
          Catalog revision {service.catalogRevision} · schema {service.schemaVersion}
        </p>
      </details>}
      {error && <div className="expression-result error">{error}</div>}
      {notice && <div className="expression-result success">{notice}</div>}
      {advanced && <section className="semantic-source">
        <h3>PostgreSQL semantic sources</h3>
        {!sourcePermitted && <p className="muted">
          Source discovery and synchronization require both semantic:inspect
          and semantic:source.
        </p>}
        {sourcePermitted && <>
          <p className="muted">
            Source registration reads only allowlisted relation and column
            metadata; it does not query table rows or values.
          </p>
          <div className="semantic-generation-controls">
            <label>
              <span>Allowlisted relation</span>
              <select
                aria-label="Semantic source relation"
                disabled={busy || generationRunning || sourceRelations.length === 0}
                value={sourceId}
                onChange={event => setSourceId(event.target.value)}
              >
                {sourceRelations.map(source => <option
                  key={source.assetId}
                  value={source.assetId}
                >
                  {source.alias}:{source.schema}.{source.relation} · {source.kind}
                </option>)}
              </select>
            </label>
            <button
              disabled={busy || generationRunning || !sourceId}
              onClick={syncSource}
            >
              Register or sync source metadata
            </button>
          </div>
          {!busy && sourceRelations.length === 0 && <p className="muted">
            No allowlisted selectable PostgreSQL relations were found.
          </p>}
          {sourceCursor && <button
            disabled={busy || generationRunning}
            onClick={() => loadMore({
              path: '/api/semantic/source/relations',
              cursor: sourceCursor,
              field: 'relations',
              setItems: setSourceRelations,
              setCursor: setSourceCursor,
            })}
          >Load more source relations</button>}
        </>}
      </section>}
      <div className="semantic-layout" aria-busy={busy || !!generating}>
        <aside className="semantic-assets">
          <label>
            <span>Find assets</span>
            <input
              aria-label="Find semantic assets"
              type="search"
              disabled={generationRunning}
              value={search}
              onChange={event => setSearch(event.target.value)}
            />
          </label>
          <div>
            {visibleAssets.map(asset => <button
              className={asset.id === selectedId ? 'active' : ''}
              disabled={generationRunning}
              key={asset.id}
              onClick={() => chooseAsset(asset.id)}
            >
              <strong>{assetName(asset)}</strong>
              <small>{asset.status} · v{asset.version}</small>
            </button>)}
            {!busy && visibleAssets.length === 0 && <p className="muted">No semantic assets found.</p>}
          </div>
          {catalogCursor && <button
            disabled={busy || generationRunning}
            onClick={() => loadMore({
              path: '/api/semantic/catalog',
              cursor: catalogCursor,
              field: 'assets',
              setItems: setAssets,
              setCursor: setCatalogCursor,
            })}
          >Load more semantic assets</button>}
        </aside>
        <div className="semantic-editor">
          {!selected && <div className="panel empty">No semantic profiles have been registered yet.</div>}
          {selected && <>
            <div className="semantic-asset-head">
              <div>
                <h3>{assetName(selected)}</h3>
                <code>{selected.id}</code>
                {!advanced && (Object.keys(selected.curated || {}).length > 0
                  ? <details className="semantic-curation-status">
                    <summary>Existing curated semantic annotations are available</summary>
                    <pre>{formatJson(selected.curated)}</pre>
                  </details>
                  : <p className="semantic-curation-status">
                    No curated semantic annotations have been saved for this layer yet.
                  </p>)}
              </div>
              <span className={`semantic-state ${selected.status}`}>{selected.status}</span>
            </div>
            {advanced && archivePermitted && selected.status === 'ready' && <button
              className="danger" disabled={busy || generationRunning} onClick={archiveSelected}
            >Archive semantic profile</button>}
            {!advanced && <div className="semantic-wizard-steps" aria-label="Semantic proposal wizard">
              <button className={wizardStep === 1 ? 'active' : ''} disabled={generationRunning} onClick={() => setWizardStep(1)}>1. Choose layer</button>
              <button
                className={wizardStep === 2 ? 'active' : ''}
                disabled={!selected || generationRunning}
                onClick={() => setWizardStep(2)}
              >2. Generate</button>
              <button
                className={wizardStep === 3 ? 'active' : ''}
                disabled={!generatedDraft || generationRunning}
                onClick={() => setWizardStep(3)}
              >3. Review</button>
              <button
                className={wizardStep === 4 ? 'active' : ''}
                disabled={!createdProposal || generationRunning}
                onClick={() => setWizardStep(4)}
              >4. Apply</button>
            </div>}
            {!advanced && wizardStep === 1 && <p className="semantic-guide">
              Choose a layer from the list to begin.
            </p>}
            {advanced && <details open>
              <summary>Generated profile</summary>
              <p className="muted">Maintained from the source or derived-layer lifecycle.</p>
              <pre>{formatJson(selected.generated)}</pre>
            </details>}
            {advanced && Object.keys(selected.curated || {}).length > 0 && <details>
              <summary>Current curated annotations</summary>
              <pre>{formatJson(selected.curated)}</pre>
            </details>}
            {advanced && Array.isArray(selected.orphans) && selected.orphans.length > 0 && <details>
              <summary>Orphaned field annotations</summary>
              <p className="muted">Retained when source fields were removed; they are never silently discarded.</p>
              <pre>{formatJson(selected.orphans)}</pre>
            </details>}
            {advanced && <details>
              <summary>Immutable asset history</summary>
              <p className="muted">Source events and curated decisions retained by catalog revision.</p>
              {history === null
                ? <button disabled={busy || generationRunning} onClick={loadHistory}>Load history</button>
                : <>
                  <pre>{formatJson(history)}</pre>
                  {historyCursor && <button
                    disabled={busy || generationRunning}
                    onClick={() => loadMore({
                      path: `/api/semantic/catalog/objects/${encodeURIComponent(selected.id)}/history`,
                      cursor: historyCursor,
                      field: 'history',
                      setItems: setHistory,
                      setCursor: setHistoryCursor,
                    })}
                  >Load more asset history</button>}
                </>}
            </details>}
            {(advanced || wizardStep === 2) && <section className="semantic-generator" aria-labelledby="semantic-generator-title">
              <h3 id="semantic-generator-title">Generate with Gemini</h3>
              {!generationAvailable && <p className="muted">
                Gemini semantic generation is not available for this deployment.
              </p>}
              {generationAvailable && !generationPermitted && <p className="muted">
                Current access requires both semantic:inspect and semantic:generate.
              </p>}
              {generationAvailable && generationPermitted && <>
                <p className="semantic-generation-intro">
                  What would you like Gemini to describe? Drafts are always reviewed before they become proposals.
                </p>
                <div className="semantic-generation-choices" role="group" aria-label="Semantic draft type">
                  {generationTargets.includes('table') && <button
                    className={generationMode === 'table' ? 'active' : ''}
                    disabled={busy || generationRunning}
                    onClick={() => setGenerationMode('table')}
                  ><strong>The layer</strong><small>Its purpose, coverage, and caveats</small></button>}
                  {generationTargets.includes('field') && <button
                    className={generationMode === 'field' ? 'active' : ''}
                    disabled={busy || generationRunning}
                    onClick={() => setGenerationMode('field')}
                  ><strong>Specific fields</strong><small>Names, meanings, and field-level guidance</small></button>}
                </div>
                {generationMode === 'table' && <div className="semantic-generation-action">
                  <p>Generate a draft for <strong>{assetName(selected)}</strong>.</p>
                  <button disabled={busy || generationRunning} onClick={() => generateDraft({kind: 'table'})}>
                    {generating === 'table' ? 'Generating layer draft…' : 'Generate layer draft'}
                  </button>
                </div>}
                {generationMode === 'field' && <div className="semantic-field-picker">
                  <div className="semantic-field-picker-head">
                    <strong>Choose up to {MAX_GENERATION_FIELDS} fields</strong>
                    <small>{generationFieldIds.length} selected</small>
                  </div>
                  {fields.length > 0
                    ? <div className="semantic-field-options">
                      {fields.map((field, index) => {
                        const checked = generationFieldIds.includes(field.id);
                        const annotation = selected.curated?.fields?.[field.id];
                        const hasSavedAnnotation = (
                          annotation
                          && typeof annotation === 'object'
                          && !Array.isArray(annotation)
                          && Object.keys(annotation).length > 0
                        );
                        const statusId = `semantic-field-status-${index}`;
                        return <label key={field.id} className={checked ? 'selected' : ''}>
                          <input
                            type="checkbox"
                            aria-describedby={hasSavedAnnotation ? statusId : undefined}
                            disabled={busy || generationRunning || (
                              !checked
                              && generationFieldIds.length >= MAX_GENERATION_FIELDS
                            )}
                            checked={checked}
                            onChange={() => {
                              const next = checked
                                ? generationFieldIds.filter(id => id !== field.id)
                                : [...generationFieldIds, field.id];
                              setGenerationFieldIds(next);
                              setCuratedText('');
                              setGeneratedDraft(null);
                              setReview(null);
                            }}
                          />
                          <span>{field.name || field.id}</span>
                          {hasSavedAnnotation && <small
                            className="semantic-field-status"
                            id={statusId}
                          >Saved semantic value</small>}
                        </label>;
                      })}
                    </div>
                    : <p className="muted">This profile has no fields with stable semantic IDs.</p>}
                  <button
                    disabled={busy || generationRunning || generationFieldIds.length === 0}
                    onClick={generateFieldDrafts}
                  >
                    {generating === 'field'
                      ? `Generating field drafts… ${generationProgress?.current ?? 0}/${generationProgress?.total || generationFieldIds.length} completed`
                      : `Generate drafts for ${generationFieldIds.length || 'selected'} field${generationFieldIds.length === 1 ? '' : 's'}`}
                  </button>
                </div>}
                <details className="semantic-generation-disclosure">
                  <summary>What Gemini receives</summary>
                  <p>Relevant table, column, and existing semantic metadata is always sent.</p>
                  {(sampleRowsCapability?.available === true
                    || statisticsCapability?.available === true) && <fieldset
                    disabled={busy || generationRunning || !generationDataPermitted}
                  >
                    <legend>Optional context</legend>
                    {!generationDataPermitted && <small className="muted">
                      Optional row samples and statistics require semantic:data.
                    </small>}
                    {sampleRowsCapability?.available === true && <label>
                      <input
                        type="checkbox"
                        disabled={busy || generationRunning || !generationDataPermitted}
                        checked={contextOptions.sampleRows}
                        onChange={event => setContextOptions(current => ({
                          ...current,
                          sampleRows: event.target.checked,
                        }))}
                      />
                      <span>5% sample of row data (capped)</span>
                      {(Number.isInteger(sampleRowsCapability.maxRows)
                        || Number.isInteger(sampleRowsCapability.maxBytes)) && <small>
                        Capped at
                        {Number.isInteger(sampleRowsCapability.maxRows)
                          ? ` ${sampleRowsCapability.maxRows} rows`
                          : ''}
                        {Number.isInteger(sampleRowsCapability.maxRows)
                          && Number.isInteger(sampleRowsCapability.maxBytes)
                          ? ' or'
                          : ''}
                        {Number.isInteger(sampleRowsCapability.maxBytes)
                          ? ` ${Math.floor(sampleRowsCapability.maxBytes / 1024)} KiB`
                          : ''}, whichever comes first.
                      </small>}
                    </label>}
                    {statisticsCapability?.available === true && <label>
                      <input
                        type="checkbox"
                        disabled={busy || generationRunning || !generationDataPermitted}
                        checked={contextOptions.statistics}
                        onChange={event => setContextOptions(current => ({
                          ...current,
                          statistics: event.target.checked,
                        }))}
                      />
                      <span>Table/column statistics</span>
                      <small>
                        Table drafts send a planner row estimate and column
                        counts. Field drafts send aggregate counts and text
                        lengths from at most{' '}
                        {Number.isInteger(statisticsCapability.fieldMaxSampledRows)
                          ? statisticsCapability.fieldMaxSampledRows.toLocaleString()
                          : '1,000'} rows in a{' '}
                        {Number.isInteger(statisticsCapability.fieldSamplePercent)
                          ? statisticsCapability.fieldSamplePercent
                          : 5}% sample; no underlying values are sent.
                      </small>
                    </label>}
                  </fieldset>}
                  <p>Semantic metadata leaves MAPP for Google&apos;s Gemini service and may be processed or retained under that Gemini project&apos;s billing and data terms. Row data and field values are sent only when the sample option is selected. Database credentials are never sent.</p>
                  <small>{generationCapability.model} · do not send any metadata or selected data context you are not authorized to disclose.</small>
                </details>
              </>}
            </section>}
            <label hidden={!advanced}>
              <span>Manual replacement JSON</span>
              <small className="muted">
                Use this Advanced editor only to replace curated annotations manually.
                Existing annotations are available above.
              </small>
              <textarea
                aria-label="Curated metadata JSON"
                rows={curatedText.trim() ? 12 : 3}
                spellCheck="false"
                disabled={generationRunning}
                placeholder={'{\n  "displayName": "…"\n}'}
                value={curatedText}
                onChange={event => {
                  setCuratedText(event.target.value);
                  setGeneratedDraft(null);
                  setReview(null);
                }}
              />
            </label>
            <label hidden={!advanced}>
              <span>Why is this change needed?</span>
              <input
                aria-label="Semantic proposal explanation"
                disabled={generationRunning}
                value={explanation}
                onChange={event => {
                  setExplanation(event.target.value);
                  setReview(null);
                }}
              />
            </label>
            {(advanced || wizardStep === 3) && generatedDraft && <section className="semantic-wizard-review">
              {!review ? <>
                <h3>Review your draft</h3>
                <p className="muted">Check the exact changes before saving a proposal.</p>
                <pre>{formatJson(generatedDraft.operations)}</pre>
                <button disabled={busy || generationRunning} onClick={prepare}>Check proposal</button>
              </> : <>
                <h3>Focused diff</h3>
                <p className="muted">These are the changes that will be saved in the proposal.</p>
                <pre>{formatJson(review.diff)}</pre>
                <button disabled={busy || generationRunning} onClick={createProposal}>Create proposal</button>
              </>}
            </section>}
            {advanced && !generatedDraft && <button disabled={busy || generationRunning} onClick={prepare}>Check proposal</button>}
            {review && !generatedDraft && <div className="semantic-review">
              <h3>Focused diff</h3>
              <pre>{formatJson(review.diff)}</pre>
              <button disabled={busy || generationRunning} onClick={createProposal}>Create proposal</button>
            </div>}
            {!advanced && wizardStep === 4 && createdProposal && <section className="semantic-wizard-review">
              <h3>Proposal ready to apply</h3>
              <p className="muted">This is the stored proposal. Applying it updates the semantic profile.</p>
              <pre>{formatJson(createdProposal.diff)}</pre>
              <button
                disabled={busy || generationRunning || proposalReview?.id !== createdProposal.id}
                onClick={() => decide(proposalReview, 'apply')}
              >Apply proposal</button>
            </section>}
          </>}
        </div>
      </div>
      {advanced && <>
      <h3>Derived profile delivery</h3>
      <div className="semantic-proposals">
        {derivedProfiles.map(profile => <div className="token-row" key={profile.name}>
          <span>
            <strong>derived_layers.{profile.name}</strong>
            <small>{profile.status} · generation {profile.generation} · revision {profile.revision ?? 'pending'}</small>
            {profile.delivery && <small>
              Blocking {profile.delivery.operation} delivery: {profile.delivery.status}
              {profile.delivery.lastError ? ` · ${profile.delivery.lastError}` : ''}
            </small>}
          </span>
          {(profile.status === 'repair_required'
            || profile.delivery?.status === 'repair_required') && <button
            disabled={busy || generationRunning}
            onClick={() => repair(profile)}
          >Retry delivery</button>}
        </div>)}
        {deliveryBlockers.map(blocker => <div
          className="token-row"
          key={blocker.eventId}
        >
          <span>
            <strong>{blocker.relation}</strong>
            <small>
              Dropped relation · blocking {blocker.operation} delivery:{' '}
              {blocker.status}
              {blocker.lastError ? ` · ${blocker.lastError}` : ''}
            </small>
          </span>
          {blocker.status === 'repair_required' && <button
            disabled={busy || generationRunning}
            onClick={() => repair(blocker)}
          >Retry delivery</button>}
        </div>)}
        {deliveryBlockersMore && <p className="muted" role="status">
          More dropped-relation delivery blockers are waiting. Repair the
          displayed blockers, then refresh the semantic catalog to retrieve
          the next batch.
        </p>}
        {!busy
          && derivedProfiles.length === 0
          && deliveryBlockers.length === 0
          && <p className="muted">No managed derived profiles.</p>}
        {derivedCursor && <button
          disabled={busy || generationRunning}
          onClick={() => loadMore({
            path: '/api/semantic/derived-profiles',
            cursor: derivedCursor,
            field: 'derivedProfiles',
            setItems: setDerivedProfiles,
            setCursor: setDerivedCursor,
            afterLoad: result => setDeliveryBlockers(current => [
              ...current,
              ...(result.deliveryBlockers || []),
            ]),
          })}
        >Load more derived profiles</button>}
      </div>
      <h3>Semantic proposals</h3>
      <div className="semantic-proposals">
        {proposals.map(proposal => <React.Fragment key={proposal.id}>
          <div className="token-row">
            <span>
              <strong>{proposal.id} · {proposal.assetId}</strong>
              <small>{proposalState(proposal)} · base version {proposal.baseVersion}</small>
            </span>
            {proposalState(proposal) === 'pending' && <div className="semantic-actions">
              <button disabled={busy || generationRunning} onClick={() => reviewProposal(proposal)}>Review</button>
              <button
                disabled={busy || generationRunning || proposalReview?.id !== proposal.id}
                onClick={() => decide(proposalReview, 'apply')}
              >Apply</button>
              <button disabled={busy || generationRunning} className="danger" onClick={() => decide(proposal, 'decline')}>Decline</button>
            </div>}
          </div>
          {proposalState(proposal) === 'applied' && <details className="semantic-proposal-details">
            <summary>Accepted proposal details</summary>
            <h4>Explanation</h4>
            <p>{proposal.explanation || 'No explanation provided.'}</p>
            <h4>Focused diff</h4>
            <pre>{formatJson(proposal.diff)}</pre>
          </details>}
        </React.Fragment>)}
        {!busy && proposals.length === 0 && <p className="muted">No semantic proposals.</p>}
        {proposalCursor && <button
          disabled={busy || generationRunning}
          onClick={() => loadMore({
            path: '/api/semantic/proposals',
            cursor: proposalCursor,
            field: 'proposals',
            setItems: setProposals,
            setCursor: setProposalCursor,
          })}
        >Load more semantic proposals</button>}
      </div>
      {proposalReview && <div className="semantic-review">
        <h3>Stored proposal evidence</h3>
        <h4>Explanation</h4>
        <p>{proposalReview.explanation || 'No explanation provided.'}</p>
        <h4>Focused diff</h4>
        <pre>{formatJson(proposalReview.diff)}</pre>
      </div>}
      </>}
    </section>
  </div>;
}
