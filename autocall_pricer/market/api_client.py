"""REST API client: fetch option prices and build OptionRecord list.

OAuth2 token acquisition and HTTP fetching are implemented.
The payload parsing section is a PLACEHOLDER — fill in once you share
a sample JSON response.  Everything else (IV solving, surface fitting)
is complete and will work unchanged once parsing is wired up.

Usage
-----
    client = OptionAPIClient(
        token_url="https://auth.example.com/oauth/token",
        api_url="https://api.example.com/options/chain",
        client_id="...",
        client_secret="...",
    )
    records = client.fetch_chain(
        underlying="AAPL",
        S0=185.0,
        r=0.05,
        q=0.01,
        reference=my_reference_lookup,   # optional
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests

from .option_chain import OptionRecord

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OAuth2 token helper
# ---------------------------------------------------------------------------

@dataclass
class _TokenCache:
    access_token: str = ""
    expires_at: float = 0.0


def _fetch_token(token_url: str, client_id: str, client_secret: str) -> str:
    """Request a client-credentials OAuth2 access token."""
    import time
    resp = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    token = body["access_token"]
    expires_in = body.get("expires_in", 3600)
    return token, time.time() + expires_in - 30  # 30 s buffer


# ---------------------------------------------------------------------------
# Reference data lookup
# ---------------------------------------------------------------------------

@dataclass
class OptionReference:
    """Static reference data mapping option_id → (strike, expiry, type).

    Populate from your static reference source (database, flat file, etc.).
    expiry should be in years from the pricing date.

    Example
    -------
    ref = OptionReference()
    ref.data["AAPL 250117C00150000"] = {
        "strike": 150.0,
        "expiry": 0.5,
        "option_type": "call",
        "underlying": "AAPL",
    }
    """
    data: dict[str, dict] = field(default_factory=dict)

    def lookup(self, option_id: str) -> dict | None:
        return self.data.get(option_id)


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class OptionAPIClient:
    """Fetches option price data from a REST API and returns OptionRecord list.

    Parameters
    ----------
    token_url     : OAuth2 token endpoint
    api_url       : option chain data endpoint
    client_id     : OAuth2 client ID
    client_secret : OAuth2 client secret
    extra_params  : additional query parameters appended to every request
    """

    def __init__(
        self,
        token_url: str,
        api_url: str,
        client_id: str,
        client_secret: str,
        extra_params: dict[str, str] | None = None,
    ) -> None:
        self._token_url = token_url
        self._api_url = api_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._extra_params = extra_params or {}
        self._cache = _TokenCache()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def fetch_chain(
        self,
        underlying: str,
        S0: float,
        r: float,
        q: float = 0.0,
        reference: OptionReference | None = None,
        request_params: dict[str, str] | None = None,
    ) -> list[OptionRecord]:
        """Fetch the option chain and return a list of OptionRecord objects.

        Parameters
        ----------
        underlying     : underlying identifier sent to the API
        S0             : current spot price
        r              : risk-free rate
        q              : dividend yield
        reference      : optional OptionReference for strike/expiry lookup
        request_params : additional params to merge into the API request
        """
        payload = self._get(underlying, request_params or {})
        return self._parse_payload(payload, underlying, S0, r, q, reference)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _token(self) -> str:
        import time
        if time.time() > self._cache.expires_at:
            token, expires_at = _fetch_token(
                self._token_url, self._client_id, self._client_secret
            )
            self._cache.access_token = token
            self._cache.expires_at = expires_at
            log.debug("OAuth2 token refreshed")
        return self._cache.access_token

    def _get(self, underlying: str, extra: dict) -> Any:
        params = {"underlying": underlying, **self._extra_params, **extra}
        headers = {"Authorization": f"Bearer {self._token()}"}
        resp = requests.get(self._api_url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  PLACEHOLDER — complete once you share a sample JSON response   ║
    # ╚══════════════════════════════════════════════════════════════════╝
    # ------------------------------------------------------------------

    def _parse_payload(
        self,
        payload: Any,
        underlying: str,
        S0: float,
        r: float,
        q: float,
        reference: OptionReference | None,
    ) -> list[OptionRecord]:
        """Parse the raw API JSON into a list of OptionRecord objects.

        CURRENT STATE: stub that raises NotImplementedError.
        Replace the body of this method once you share a sample payload.

        What to fill in
        ---------------
        The payload is expected to be a dict of:
            { option_id: { "bid": ..., "ask": ..., <maybe strike/expiry> } }

        Two scenarios for strike and expiry:

        Scenario A — strike/expiry are IN the payload:
            for option_id, quote in payload.items():
                records.append(OptionRecord(
                    option_id   = option_id,
                    underlying  = underlying,
                    option_type = quote["option_type"],   # adapt field name
                    strike      = float(quote["strike"]), # adapt field name
                    expiry      = float(quote["expiry"]), # in years — convert if date string
                    bid         = float(quote["bid"]),
                    ask         = float(quote["ask"]),
                    S0=S0, r=r, q=q,
                ))

        Scenario B — strike/expiry come from a reference lookup:
            for option_id, quote in payload.items():
                ref = reference.lookup(option_id) if reference else None
                if ref is None:
                    log.warning("No reference data for %s — skipped", option_id)
                    continue
                records.append(OptionRecord(
                    option_id   = option_id,
                    underlying  = underlying,
                    option_type = ref["option_type"],
                    strike      = ref["strike"],
                    expiry      = ref["expiry"],
                    bid         = float(quote["bid"]),
                    ask         = float(quote["ask"]),
                    S0=S0, r=r, q=q,
                ))
        """
        # ----------------------------------------------------------------
        # TODO: replace this with real parsing once API format is known
        # ----------------------------------------------------------------
        raise NotImplementedError(
            "API payload parser is not yet implemented.\n"
            "Share a sample JSON response and fill in _parse_payload().\n"
            "See the docstring above for the two common patterns."
        )
