import {useState, useEffect} from 'react';

// Provision and retire change what the platform serves deliberately, so each
// is confirmed separately rather than by a single "are you sure" for the
// panel. Observe is not merely a probe: _persist_observation() re-applies
// consumer access from the evidence it just gathered, so a drifted or failed
// observation revokes the map reader and derived owner, and a good one
// restores them. It stays unconfirmed because that is the correcting action,
// but the panel says so rather than presenting it as read-only.
const CONFIRMABLE = new Set(['provision', 'retire']);

const ACKNOWLEDGEMENTS = [
  {
    property: 'physicalRebindAcknowledged',
    label: 'Different physical database',
    help: 'A restored backup or swapped host keeps every name and column identical. This is the only signal that the source is not the one previously approved.',
  },
  {
    property: 'schemaChangeAcknowledged',
    label: 'Schema fingerprint moved',
    help: 'The relation shapes changed since the accepted observation.',
  },
  {
    property: 'rowLevelSecurityAcknowledged',
    label: 'Row-level security in force',
    help: 'What MAPP sees depends on the reading role, so the exposed rows may be a subset.',
  },
];

export function evidenceState(alias) {
  if (!alias || alias.status === 'retired') return 'archived';
  if (!alias.provisionedAt) return 'registered';
  return alias.acceptedEvidenceComplete ? 'verified' : 'incomplete';
}

// The status a successful action must leave behind. Anything else means the
// response did not establish what committed, so it is not reported as success.
export function expectedStatus(action) {
  return action === 'retire' ? 'retired' : 'active';
}

export function actionOutcome(action, alias, requested) {
  if (!alias || typeof alias !== 'object') return 'indeterminate';
  if (alias.alias !== requested) return 'indeterminate';
  if (action === 'observe') return 'observed';
  return alias.status === expectedStatus(action) ? 'committed' : 'indeterminate';
}

function StatusBadge({status}) {
  return <span className={`federation-status federation-status-${status}`}>{status}</span>;
}

function Evidence({alias}) {
  const state = evidenceState(alias);
  const observation = alias.lastObservation || {};
  return <dl className="federation-evidence">
    <dt>Evidence</dt>
    <dd>{{
      verified: 'Accepted fingerprint, physical identity, and connection identity all recorded.',
      incomplete: 'Provisioned before the full evidence set was recorded. Observe again before relying on it.',
      registered: 'Registered only. Nothing is exposed until it is provisioned.',
      archived: 'Archived. Objects were renamed out of the way, not dropped.',
    }[state]}</dd>
    <dt>Connection</dt>
    <dd>{alias.connectionRef} · TLS {alias.tlsPolicy}</dd>
    <dt>Relations</dt>
    <dd>{(alias.allowedRelations || []).join(', ') || 'none'}</dd>
    <dt>Last observation</dt>
    <dd>
      {observation.connectivity
        ? `${observation.connectivity} · schema ${observation.schema || 'unknown'}`
        : 'never observed'}
      {alias.lastObservationId ? ` · id ${alias.lastObservationId}` : ''}
    </dd>
    {observation.schemaFingerprint && <>
      <dt>Schema vs accepted</dt>
      <dd>
        {observation.acceptedSchemaCurrent === false
          ? 'Differs from the accepted fingerprint. Provisioning needs the schema-change acknowledgement.'
          : 'Matches the accepted fingerprint.'}
        <br/><small>{observation.schemaFingerprint.slice(0, 24)}…</small>
      </dd>
    </>}
    {observation.rowLevelSecurityDetected !== undefined && <>
      <dt>Row-level security</dt>
      <dd>
        {observation.rowLevelSecurityDetected
          ? 'Detected on the source. What MAPP sees depends on the reading role.'
          : 'Not detected on the source.'}
      </dd>
    </>}
    {alias.provisionedAt && <>
      <dt>Approved</dt>
      <dd>{alias.approvedBy || 'unknown'} at {alias.approvedAt || alias.provisionedAt}</dd>
    </>}
    {alias.retiredAt && <>
      <dt>Retired</dt>
      <dd>
        {alias.retiredBy || 'unknown'} at {alias.retiredAt}
        {alias.archivedSchema ? ` · schema ${alias.archivedSchema}` : ''}
        {alias.archivedServer ? ` · server ${alias.archivedServer}` : ''}
      </dd>
    </>}
    <dt>Handling</dt>
    <dd>{alias.dataHandlingClassification || 'not classified'}</dd>
  </dl>;
}

export function FederatedSources({api, close}) {
  const [aliases, setAliases] = useState([]);
  const [selectedAlias, setSelectedAlias] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [pending, setPending] = useState(null);
  const [acknowledged, setAcknowledged] = useState([]);
  // Distinct from 'a request finished': a failed list leaves the registry
  // unknown, which must not render as a confirmed empty one.
  const [listed, setListed] = useState(false);

  const load = async keepSelection => {
    try {
      const result = await api('/api/federation/aliases');
      const items = result.aliases || [];
      setAliases(items);
      if (!keepSelection || !items.some(item => item.alias === keepSelection)) {
        setSelectedAlias(items.length ? items[0].alias : '');
      }
      setListed(true);
    } catch (err) {
      setError(err.message);
      setListed(false);
    }
  };

  useEffect(() => {
    load('');
  }, []);

  const selected = aliases.find(item => item.alias === selectedAlias) || null;

  const start = action => {
    setError('');
    setNotice('');
    setAcknowledged([]);
    if (CONFIRMABLE.has(action)) setPending(action);
    else run(action, []);
  };

  const toggleAcknowledgement = (property, checked) => {
    setAcknowledged(current => checked
      ? [...current, property]
      : current.filter(item => item !== property));
  };

  const run = async (action, acknowledgements) => {
    if (!selected) return;
    const alias = selected.alias;
    setBusy(true);
    setError('');
    setNotice('');
    setPending(null);
    const body = {};
    if (action === 'provision') {
      // Bound to the observation actually read, so provisioning cannot
      // approve a state nobody looked at.
      body.expectedObservationId = selected.lastObservationId;
      acknowledgements.forEach(property => {
        body[property] = true;
      });
    }
    try {
      const result = await api(`/api/federation/aliases/${encodeURIComponent(alias)}/${action}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      const outcome = actionOutcome(action, result.alias, alias);
      if (outcome === 'indeterminate') {
        setError(
          `The ${action} response did not establish what committed for ${alias}. `
          + 'Do not repeat it; reload and check the alias state.',
        );
      } else if (outcome === 'observed') {
        setNotice(`Observed ${alias}.`);
      } else {
        setNotice(`${alias} is now ${expectedStatus(action)}.`);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
      setAcknowledged([]);
      await load(alias);
    }
  };

  const canProvision = selected
    && selected.status !== 'retired'
    && selected.lastObservationId !== null
    && selected.lastObservationId !== undefined;

  return <div className="modal-backdrop">
    <section className="panel security-panel federation-panel">
      <div className="form-head">
        <div>
          <h2>Federated sources</h2>
          <p>
            Register records intent, observe probes the source live, and provision is
            the only step that deliberately serves its data.
          </p>
          <p className="muted">
            Observe is not read-only: it re-applies consumer access from what it
            finds, so an observation that shows drift revokes the map reader and
            derived owner, and a clean one restores them.
          </p>
        </div>
        <button disabled={busy} onClick={close}>Close</button>
      </div>
      {error && <div className="expression-result error">{error}</div>}
      {notice && <div className="expression-result success">{notice}</div>}

      {listed && !aliases.length && <div className="panel empty">
        No active sources. Retired aliases are deliberately omitted here, and
        their names stay reserved, so this is not proof the registry is empty —
        {' '}<code>config-cli federation show &lt;alias&gt;</code> still returns a
        retired one by exact name. To attach a source, use
        {' '}<code>config-cli federation register</code>, which needs a
        {' '}<code>FEDERATION_DBS_&lt;REF&gt;</code> credential already present in the
        environment.
      </div>}

      {!!aliases.length && <div className="federation-body">
        <nav className="federation-list">
          {aliases.map(alias => <button
            key={alias.alias}
            className={alias.alias === selectedAlias ? 'active' : ''}
            onClick={() => {
              setSelectedAlias(alias.alias);
              setPending(null);
              setError('');
              setNotice('');
            }}
          >
            <span>{alias.displayName || alias.alias}</span>
            <StatusBadge status={alias.status}/>
          </button>)}
        </nav>

        {selected && <div className="federation-detail">
          <h3>{selected.alias}</h3>
          <Evidence alias={selected}/>

          {pending === 'provision' && <div className="federation-confirm">
            <p>
              Provisioning exposes {(selected.allowedRelations || []).length} relation(s)
              to the map reader and the derived-layer owner, bound to observation
              {' '}{selected.lastObservationId}.
            </p>
            <p className="muted">
              Tick only what an operator has actually decided. The server refuses
              rather than assuming.
            </p>
            {ACKNOWLEDGEMENTS.map(item => <label className="federation-ack" key={item.property}>
              <input
                type="checkbox"
                checked={acknowledged.includes(item.property)}
                onChange={event => toggleAcknowledgement(item.property, event.target.checked)}
              />
              <span><strong>{item.label}</strong><small>{item.help}</small></span>
            </label>)}
            <div className="federation-buttons">
              <button disabled={busy} onClick={() => run('provision', acknowledged)}>
                Provision and expose
              </button>
              <button disabled={busy} className="secondary" onClick={() => setPending(null)}>
                Cancel
              </button>
            </div>
          </div>}

          {pending === 'retire' && <div className="federation-confirm">
            <p>
              Retiring revokes access, then renames the schema, server and
              foreign tables out of the way. The registry row and its
              observation history are kept as the audit trail.
            </p>
            <p>
              <strong>The user mappings are dropped.</strong> Those hold the
              remote credential in the PostgreSQL catalogue, so the archived
              server cannot reach the source again. Re-attaching this data means
              registering a new alias with a fresh credential.
            </p>
            <p className="muted">
              This is terminal: the alias leaves this list, and the same name
              cannot be registered again. It is refused while a derived or
              workspace layer still reads the source.
            </p>
            <div className="federation-buttons">
              <button disabled={busy} className="danger" onClick={() => run('retire', [])}>
                Retire and archive
              </button>
              <button disabled={busy} className="secondary" onClick={() => setPending(null)}>
                Cancel
              </button>
            </div>
          </div>}

          {!pending && <div className="federation-buttons">
            <button disabled={busy} onClick={() => start('observe')}>
              {busy ? 'Working…' : 'Observe'}
            </button>
            <button
              disabled={busy || !canProvision}
              title={canProvision ? undefined : 'Observe the source first.'}
              onClick={() => start('provision')}
            >
              Provision
            </button>
            <button
              disabled={busy || selected.status === 'retired'}
              className="danger"
              onClick={() => start('retire')}
            >
              Retire
            </button>
          </div>}
        </div>}
      </div>}
    </section>
  </div>;
}
