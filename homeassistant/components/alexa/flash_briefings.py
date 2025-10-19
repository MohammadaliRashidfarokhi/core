"""Support for Alexa skill service end point."""

import hmac
from http import HTTPStatus
import logging
import uuid

from aiohttp.web_response import StreamResponse

from homeassistant.components import http
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import template
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    API_PASSWORD,
    ATTR_MAIN_TEXT,
    ATTR_REDIRECTION_URL,
    ATTR_STREAM_URL,
    ATTR_TITLE_TEXT,
    ATTR_UID,
    ATTR_UPDATE_DATE,
    CONF_AUDIO,
    CONF_DISPLAY_URL,
    CONF_TEXT,
    CONF_TITLE,
    CONF_UID,
    DATE_FORMAT,
)

_LOGGER = logging.getLogger(__name__)

FLASH_BRIEFINGS_API_ENDPOINT = "/api/alexa/flash_briefings/{briefing_id}"


@callback
def async_setup(hass: HomeAssistant, flash_briefing_config: ConfigType) -> None:
    """Activate Alexa component."""
    hass.http.register_view(AlexaFlashBriefingView(hass, flash_briefing_config))


class AlexaFlashBriefingView(http.HomeAssistantView):
    """Handle Alexa Flash Briefing skill requests."""

    url = FLASH_BRIEFINGS_API_ENDPOINT
    requires_auth = False
    name = "api:alexa:flash_briefings"

    def __init__(self, hass: HomeAssistant, flash_briefings: ConfigType) -> None:
        """Initialize Alexa view."""
        super().__init__()
        self.flash_briefings = flash_briefings

    @callback
    def get(
        self, request: http.HomeAssistantRequest, briefing_id: str
    ) -> StreamResponse | tuple[bytes, HTTPStatus]:
        """Handle Alexa Flash Briefing request."""
        _LOGGER.debug("Received Alexa flash briefing request for: %s", briefing_id)

        def _unauthorized(msg: str) -> tuple[bytes, HTTPStatus]:
            _LOGGER.error(msg, briefing_id)
            return b"", HTTPStatus.UNAUTHORIZED

        def _not_found(msg: str) -> tuple[bytes, HTTPStatus]:
            _LOGGER.error(msg, briefing_id)
            return b"", HTTPStatus.NOT_FOUND

        def _render_field(src: dict, conf_key: str, out: dict, attr_key: str) -> None:
            """Render a (possibly templated) field into output if present."""
            val = src.get(conf_key)
            if val is None:
                return
            out[attr_key] = (
                val.async_render(parse_result=False)
                if isinstance(val, template.Template)
                else val
            )


        # Auth
        supplied = request.query.get(API_PASSWORD)
        if supplied is None:
            return _unauthorized("No password provided for Alexa flash briefing: %s")

        if not hmac.compare_digest(
            supplied.encode("utf-8"),
            self.flash_briefings[CONF_PASSWORD].encode("utf-8"),
        ):
            return _unauthorized("Wrong password for Alexa flash briefing: %s")

        # Look up briefing config; must be a list (per tests)
        items = self.flash_briefings.get(briefing_id)
        if not isinstance(items, list):
            return _not_found("No configured Alexa flash briefing was found for: %s")

        # Build response
        briefing = []
        now = dt_util.utcnow().strftime(DATE_FORMAT)

        for item in items:
            output: dict = {}

            _render_field(item, CONF_TITLE, output, ATTR_TITLE_TEXT)
            _render_field(item, CONF_TEXT, output, ATTR_MAIN_TEXT)
            _render_field(item, CONF_AUDIO, output, ATTR_STREAM_URL)
            _render_field(item, CONF_DISPLAY_URL, output, ATTR_REDIRECTION_URL)

            output[ATTR_UID] = item.get(CONF_UID) or str(uuid.uuid4())
            output[ATTR_UPDATE_DATE] = now

            briefing.append(output)

        return self.json(briefing)
