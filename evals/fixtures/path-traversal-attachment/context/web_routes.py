"""Request routing."""

from __future__ import annotations

from web.attachments import read_attachment


def download_attachment(request):
    """GET /attachments/{name}

    `name` is the raw, still-percent-decoded path segment from the URL. No
    normalisation or validation happens in the router; the storage layer owns
    it.
    """
    tenant_id = request.session["tenant_id"]
    requested_name = request.path_params["name"]
    return {
        "content_type": "application/octet-stream",
        "body": read_attachment(tenant_id, requested_name),
    }
