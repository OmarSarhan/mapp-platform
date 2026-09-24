"""Bounded approval diagnostics. Never log credentials, URLs, packets or bodies."""
from contextvars import ContextVar
import json
import logging
import secrets
from mcp.server.mcpserver.exceptions import ToolError

TRACE = ContextVar('approval_trace', default=None)
LOGGER = logging.getLogger('mapp.approval')
SAFE_FIELDS = frozenset({'correlationId', 'tool', 'phase', 'operationId', 'proposalId',
    'candidateHash', 'originalRevision', 'evidenceOperationId', 'evidenceFingerprint',
    'approvalReference', 'requestDigest', 'elicitationId', 'mode', 'clientModes',
    'confirmationId', 'confirmationSchema', 'action', 'approve', 'exceptionType', 'rpcCode', 'requestId', 'status', 'code'})


def trace(event, **facts):
    current = TRACE.get() or {}
    safe = {key: value for key, value in {**current, **facts}.items()
            if key in SAFE_FIELDS and isinstance(value, (str, int, bool, list, type(None)))}
    LOGGER.info(json.dumps({'event': event, **safe}, separators=(',', ':')))


class DiagnosticToolError(ToolError):
    def __init__(self, message, *, code, diagnostics=None):
        current = TRACE.get() or {}
        self.detail = {**(diagnostics or {}), 'code': code, 'message': message,
                       'correlationId': current.get('correlationId') or secrets.token_hex(16),
                       'phase': current.get('phase', 'tool'),
                       'applyAttempted': current.get('phase') == 'apply'}
        super().__init__(message)


class ApprovalError(DiagnosticToolError):
    def __init__(self, code, message, *, approval_url=None, action=None):
        current = TRACE.get() or {}
        self.detail = {'code': code, 'message': message,
                       'correlationId': current.get('correlationId') or secrets.token_hex(16),
                       'applyAttempted': False}
        for key in ('phase', 'approvalReference', 'mode', 'elicitationId'):
            if current.get(key) is not None:
                self.detail[key] = current[key]
        if action is not None:
            self.detail['action'] = action
        if approval_url:
            self.detail['approvalUrl'] = approval_url
            self.detail['nextStep'] = 'Open the approval page and explicitly decide; after approval, retry the same tool arguments to collect the decision.'
        # Direct callers retain a diagnosable error; the wire adapter provides
        # the same object as structuredContent and text, with isError=true.
        ToolError.__init__(self, json.dumps(self.detail, separators=(',', ':')))
