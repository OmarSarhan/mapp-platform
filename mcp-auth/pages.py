"""The two HTML surfaces: consent and login.

The configuration service has no HTML response path at all — ``_json`` is its
only response helper and a grep for ``text/html`` across it returns nothing — so
there is no in-repo precedent to copy. That makes minimalism the right stance:
no template engine, no JavaScript, two plain forms.

Zero JavaScript is what makes ``default-src 'none'`` honest and removes the whole
DOM-based class of problems from the most security-sensitive screen in the
design. Every interpolated value goes through ``html.escape(..., quote=True)``;
``redirect_uri`` is shown as escaped text and never emitted as an href.
"""

from __future__ import annotations

import base64
import html
import secrets

#: 'form-action' has no default-src fallback in CSP3, so it is stated explicitly.
#:
#: The CSS is inline because P1's four-path edge budget leaves no room for a
#: stylesheet route. It carries a nonce rather than 'unsafe-inline' as defence
#: in depth, not because a concrete bypass is known: these pages run no script,
#: interpolate nothing attacker-controlled into the document, and style-src
#: permits no URL, so injected CSS would have no exfiltration channel. The
#: nonce's real value is that it stops being correct the moment someone adds an
#: interpolation, which 'unsafe-inline' would silently tolerate.
#:
#: Note 'form-action self' sits on the critical path: the consent POST answers
#: with a 302 to the client's cross-origin redirect_uri, and some engines have
#: historically applied form-action across that redirect. The test harness
#: drives http.client, which enforces no CSP, so this is unverified in a real
#: browser -- see the open item in the review notes.
CSP_TEMPLATE = (
    "default-src 'none'; style-src 'nonce-{nonce}'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)

STYLE = """
:root { color-scheme: light dark }
body { font: 16px/1.5 system-ui, sans-serif; max-width: 34rem; margin: 4rem auto; padding: 0 1rem }
h1 { font-size: 1.3rem; margin-bottom: .25rem }
p.sub { color: #666; margin-top: 0 }
dl { border: 1px solid #ccd; padding: .75rem 1rem; border-radius: 4px }
dt { font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; color: #667 }
dd { margin: 0 0 .75rem; word-break: break-all; font-family: ui-monospace, monospace }
dd:last-child { margin-bottom: 0 }
ul.scopes { padding-left: 1.1rem }
label { display: block; margin: 1rem 0 .25rem }
input[type=password] { width: 100%; padding: .5rem; font: inherit }
.actions { display: flex; gap: .5rem; margin-top: 1.5rem }
button { font: inherit; padding: .5rem 1rem; cursor: pointer }
button.primary { font-weight: 600 }
.error { color: #a3261a; font-weight: 600 }
"""


def new_nonce() -> str:
    return base64.b64encode(secrets.token_bytes(16)).decode()


def content_security_policy(nonce: str) -> str:
    return CSP_TEMPLATE.format(nonce=nonce)


def _document(*, title: str, nonce: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<style nonce="{html.escape(nonce, quote=True)}">{STYLE}</style>'
        f"</head><body>{body}</body></html>\n"
    )


def login_page(*, request_id: str, csrf: str, nonce: str, error: str | None = None) -> str:
    """The administrator login form.

    Needed because the platform has no browser login page: config-ui
    authenticates through a JSON POST from its own dashboard, and its
    ``mapp_session`` cookie is host-only and SameSite=Strict, so it will not ride
    a top-level navigation arriving from an agent's browser handoff.
    """
    banner = f'<p class="error">{html.escape(error)}</p>' if error else ""
    body = (
        "<h1>MAPP administrator sign-in</h1>"
        '<p class="sub">An agent has requested access to this MAPP instance.</p>'
        f"{banner}"
        '<form method="post" action="/oauth/login">'
        f'<input type="hidden" name="rid" value="{html.escape(request_id, quote=True)}">'
        f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
        '<label for="password">Administrator password</label>'
        '<input id="password" name="password" type="password" autocomplete="current-password" required>'
        '<div class="actions"><button class="primary" type="submit">Sign in</button></div>'
        "</form>"
    )
    return _document(title="Sign in — MAPP", nonce=nonce, body=body)


def consent_page(
    *,
    request_id: str,
    csrf: str,
    nonce: str,
    client_name: str,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
) -> str:
    """The grant decision.

    Scopes are listed one per line rather than as a space-joined string, because
    the operator is being asked to authorise each of them and a run-together
    string is how over-broad grants get waved through.
    """
    items = "".join(f"<li><code>{html.escape(scope)}</code></li>" for scope in scopes) or "<li>none</li>"
    body = (
        "<h1>Authorise this client?</h1>"
        f'<p class="sub">{html.escape(client_name)} is requesting access to this MAPP instance.</p>'
        "<dl>"
        f"<dt>Client</dt><dd>{html.escape(client_id)}</dd>"
        f"<dt>Redirect target</dt><dd>{html.escape(redirect_uri)}</dd>"
        "</dl>"
        "<p>It will be granted exactly these scopes:</p>"
        f'<ul class="scopes">{items}</ul>'
        '<form method="post" action="/oauth/authorize">'
        f'<input type="hidden" name="rid" value="{html.escape(request_id, quote=True)}">'
        f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
        '<div class="actions">'
        '<button class="primary" type="submit" name="decision" value="allow">Authorise</button>'
        '<button type="submit" name="decision" value="deny">Deny</button>'
        "</div></form>"
    )
    return _document(title="Authorise — MAPP", nonce=nonce, body=body)
