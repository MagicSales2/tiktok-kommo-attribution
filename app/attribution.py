from __future__ import annotations

import re
from urllib.parse import quote

from .db import execute, fetchrow
from .refcode import generate_ref


def build_whatsapp_url(number: str, template: str, ref: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        raise ValueError("WHATSAPP_NUMBER no está configurado")
    text = (template or "Hola, quiero información. {ref}").replace("{ref}", ref)
    if ref not in text:
        text = f"{text} {ref}"
    return f"https://wa.me/{digits}?text={quote(text, safe='')}"


async def store_click(
    *,
    ttclid: str | None,
    ttp: str | None,
    utm_source: str | None,
    utm_medium: str | None,
    utm_campaign: str | None,
    utm_content: str | None,
    utm_term: str | None,
    campaign_id: str | None,
    adgroup_id: str | None,
    ad_id: str | None,
    referrer: str | None,
    landing_url: str | None,
    user_agent: str | None,
    ip: str | None,
) -> str:
    code = generate_ref()
    await execute(
        """
        INSERT INTO attribution_clicks (
            code, ttclid, ttp,
            utm_source, utm_medium, utm_campaign, utm_content, utm_term,
            campaign_id, adgroup_id, ad_id,
            referrer, landing_url, user_agent, ip
        ) VALUES (
            $1, $2, $3,
            $4, $5, $6, $7, $8,
            $9, $10, $11,
            $12, $13, $14, $15
        )
        """,
        code,
        ttclid,
        ttp,
        utm_source,
        utm_medium,
        utm_campaign,
        utm_content,
        utm_term,
        campaign_id,
        adgroup_id,
        ad_id,
        referrer,
        landing_url,
        user_agent,
        ip,
    )
    return code


async def resolve_click(code: str) -> dict | None:
    row = await fetchrow("SELECT * FROM attribution_clicks WHERE code = $1", code)
    return dict(row) if row else None


async def link_click_to_lead(code: str, lead_id: int) -> dict | None:
    """Vincula el clic con el lead. No roba un clic ya asignado a otro lead."""
    row = await fetchrow(
        """
        UPDATE attribution_clicks
           SET lead_id = $2,
               used_at = COALESCE(used_at, now())
         WHERE code = $1
           AND (lead_id IS NULL OR lead_id = $2)
        RETURNING *
        """,
        code,
        lead_id,
    )
    return dict(row) if row else None
