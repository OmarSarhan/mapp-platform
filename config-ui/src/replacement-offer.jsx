import React, {useEffect, useMemo, useRef, useState} from 'react';

const completeChanges = changes => ['added','removed','changed'].every(key =>
  Array.isArray(changes?.[key]) && changes[key].every(field => typeof field === 'string'));

// Carry outstanding changes forward when a second replacement commits before
// the first offer is accepted. Never carry changes across an asset recreation.
export function enqueueReplacement(offers, result, workspace) {
  if (!result?.name) return offers;
  const profile = result.semanticProfile;
  const previous = offers.find(offer => offer.result.name === result.name &&
    offer.result.semanticProfile?.assetId === profile?.assetId);
  let baseline = workspace;
  let firstGeneration = profile?.generation;
  let gap = false;
  if (previous && profile?.assetId) {
    if (previous.result.semanticProfile.generation >= profile.generation) return offers;
    const contiguous = previous.result.semanticProfile.generation + 1 === profile.generation;
    gap = previous.gap || !contiguous;
    if (contiguous && completeChanges(previous.result.columnChanges) && completeChanges(result.columnChanges)) {
      const first = previous.result.columnChanges;
      const last = result.columnChanges;
      const added = new Set([...first.added, ...last.added]);
      const removed = new Set([...first.removed, ...last.removed]);
      const changed = new Set([...first.changed, ...last.changed]);
      for (const field of added) {
        if (!removed.has(field)) continue;
        added.delete(field);
        removed.delete(field);
        if (last.added.includes(field)) changed.add(field);
        else changed.delete(field);
      }
      result = {...result, columnChanges: {
        added: [...added], removed: [...removed], changed: [...changed].filter(name => !added.has(name) && !removed.has(name)),
      }};
      baseline = previous.baseline;
      firstGeneration = previous.firstGeneration;
    }
  }
  // An old incarnation can report completion late. Recheck both identities by name.
  const others = offers.filter(offer => offer !== previous).map(offer =>
    offer.result.name === result.name ? {...offer, checkVersion:(offer.checkVersion || 0) + 1} : offer);
  return [...others, {result, baseline, firstGeneration, gap}];
}

async function replacementMetadata(api, result) {
  const expected = result.semanticProfile;
  if (!expected?.assetId || !Number.isInteger(expected.generation) || !completeChanges(result.columnChanges)) {
    return {message: 'The operation returned incomplete replacement metadata. Review the relation manually.'};
  }
  const {derivedProfile} = await api(`/api/semantic/derived-profiles/${encodeURIComponent(result.name)}`);
  if (!derivedProfile?.assetId || !Number.isInteger(derivedProfile.generation)) {
    throw new Error('The current generation could not be verified.');
  }
  if (derivedProfile.assetId !== expected.assetId || derivedProfile.generation > expected.generation) {
    return {message: 'This replacement was superseded. Inspect the current definition before editing the workspace.'};
  }
  if (derivedProfile.generation !== expected.generation || derivedProfile.status !== 'ready') {
    const repair = derivedProfile.status === 'repair_required' ? ' A semantic administrator can inspect and repair delivery.' : '';
    return {retry: true, message: `Waiting for ready metadata. Current profile status: ${derivedProfile.status}.${repair}`};
  }
  const {asset} = await api(`/api/semantic/catalog/objects/${encodeURIComponent(expected.assetId)}`);
  const binding = asset?.generated?.binding;
  if (asset?.id !== expected.assetId || asset.generation !== expected.generation || asset.status !== 'ready' ||
      binding?.schema !== 'derived_layers' || binding.relation !== result.name || !Array.isArray(asset.generated.fields)) {
    throw new Error('The catalog object does not match this ready replacement generation.');
  }
  const fields = asset.generated.fields;
  const names = new Set(fields.map(field => field?.name));
  const {added, changed, removed} = result.columnChanges;
  if (fields.some(field => !field?.name || !field.type) || [...added, ...changed].some(name => !names.has(name)) ||
      removed.some(name => names.has(name))) {
    throw new Error('The generated fields do not match the reported column changes.');
  }
  return {columns: fields};
}

export function ReplacementOffer({api, offer, workspace, reconcile, apply, dismiss, busy}) {
  const {result, baseline, firstGeneration} = offer;
  const [metadata, setMetadata] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [checking, setChecking] = useState(false);
  const version = useRef(0);
  useEffect(() => {
    let current = true, timer;
    async function read() {
      let next;
      try { next = await replacementMetadata(api, result); }
      catch (error) { next = {retry: true, message: error.message}; }
      if (!current) return;
      setMetadata(next);
      if (next.retry) timer = setTimeout(read, 10000);
    }
    setMetadata(null);
    setChecking(false);
    read();
    return () => { current = false; version.current++; clearTimeout(timer); };
  }, [api, result, attempt, offer.checkVersion]);

  const proposal = useMemo(() => metadata?.columns && workspace
    ? reconcile(workspace, result, metadata.columns, baseline) : null,
  [workspace, result, metadata, baseline, reconcile]);
  async function accept() {
    const started = version.current;
    setChecking(true);
    try {
      // The offer can sit open while another administrator replaces the relation.
      const verified = await replacementMetadata(api, result);
      if (version.current !== started) return;
      if (verified.columns && proposal) apply(workspace, proposal);
      else { setMetadata(verified); if (verified.retry) setAttempt(n => n + 1); }
    } catch (error) {
      if (version.current === started) setMetadata({retry: true, message: error.message});
    } finally { if (version.current === started) setChecking(false); }
  }
  const profile = result.semanticProfile;
  const changes = result.columnChanges;
  return <section className="subpanel" aria-label={`Replacement of ${result.name}`}>
    <h3>Review workspace impact for derived_layers.{result.name}</h3>
    <p>The database replacement committed. {profile && <>Generation {profile.generation} · {profile.status} at completion.</>}</p>
    {firstGeneration < profile?.generation && <p>Includes outstanding changes since generation {firstGeneration}.</p>}
    {offer.gap && <p>Intermediate generations were not observed. Review earlier changes manually.</p>}
    {completeChanges(changes) && <p>Added: {changes.added.join(', ') || 'none'}. Removed: {changes.removed.join(', ') || 'none'}. Type changed: {changes.changed.join(', ') || 'none'}.</p>}
    <p>Values can have a new meaning even when columns match. Review feature IDs, filters, hover text and symbology, including zoom-dependent layers. Review named-locale overrides through the API or CLI.</p>
    {result.consumerLabels?.length > 0 && <p>Direct workspace consumers: {result.consumerLabels.join(', ')}.</p>}
    {result.fieldReferences?.length > 0 && <ul>{result.fieldReferences.map(item => <li key={item.path}>{item.label}</li>)}</ul>}
    <p role="status">{metadata?.message || (proposal ? 'Matching generation is ready.' : 'Checking replacement metadata…')}</p>
    {metadata?.retry && <button type="button" onClick={() => setAttempt(n => n + 1)}>Check metadata again</button>}
    {proposal && <p>{proposal.summary.layers} default workspace layer(s) have suggested edits. {proposal.summary.added} information field(s) to add, {proposal.summary.removed} to remove. Review affected filters, hover and themes before saving.</p>}
    {!!proposal?.summary.skipped.length && <p>Edited locally since replacement, excluded from suggestions: {proposal.summary.skipped.join(', ')}.</p>}
    {!!proposal?.summary.layers && <button type="button" disabled={busy || checking} onClick={accept}>Use suggested unsaved edits</button>}
    <button type="button" disabled={checking} onClick={dismiss}>Dismiss workspace review</button>
  </section>;
}
