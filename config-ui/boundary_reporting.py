"""Describe proven boundary behavior without inferring arbitrary SQL semantics."""
def source_relations(layer):
    return sorted({value for value in [layer.get('table'), *(layer.get('tables') or {}).values()]
                   if isinstance(value, str)})


def boundary_report(locale, layer, scopes=None):
    scopes = scopes or {}
    extent = locale.get('extent') or {}
    selection = []
    for relation in source_relations(layer):
        scope = scopes.get(relation)
        if isinstance(scope, dict) and scope.get('selection') == 'intersects-output-geometry':
            selection.append({'relation': relation, 'boundary': scope,
                              'predicate': 'intersects', 'clipsGeometry': False})
    return {
        'relations': source_relations(layer),
        'studyBoundary': {'status': 'unknown', 'reason': 'No verified study-boundary contract is attached to this layer. Layer names and drawn circles do not establish clipping.'},
        'featureSelection': {'status': 'configured' if selection else 'unknown', 'platformScopes': selection,
                             'description': 'Platform scopes select whole intersecting features; they do not clip geometry. Query-internal spatial predicates are not inferred.'},
        'geometryClipping': {'status': 'unknown', 'description': 'The derived map-extent wrapper does not clip geometry. Source SQL may perform its own clipping; this has not been verified.'},
        'visualMask': {'status': 'configured' if extent.get('mask') is True else 'not-configured',
                       'boundary': {key: extent[key] for key in ('north', 'east', 'south', 'west') if key in extent},
                       'description': 'A locale extent mask only changes map presentation; it does not restrict source rows or prove study-boundary clipping.'},
    }
