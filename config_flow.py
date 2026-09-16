from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_DEVICE_ID,
    CONF_DEVICE_SN,
    CONF_DEVICE_NAME,
    CONF_HTTP_TOKEN,
)
from .core.api_client import LumentreeHttpApiClient
from .core.exceptions import AuthException, ApiException

_LOGGER = logging.getLogger(__name__)


class LumentreeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Lumentree config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize config flow."""
        self._device_id_input: Optional[str] = None
        self._device_sn_from_api: Optional[str] = None
        self._device_name: Optional[str] = None
        self._http_token: Optional[str] = None
        self._reauth_entry: Optional[config_entries.ConfigEntry] = None
        self._api: Optional[LumentreeHttpApiClient] = None

    @property
    def api(self) -> LumentreeHttpApiClient:
        """Return API client."""
        if self._api is None:
            session = async_get_clientsession(self.hass)
            self._api = LumentreeHttpApiClient(session)
        return self._api

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            self._device_id_input = user_input[CONF_DEVICE_ID].strip()

            if not self._device_id_input:
                errors["base"] = "invalid_device_id"
            else:
                try:
                    # Authenticate with Lumentree server
                    self._http_token = await self.api.authenticate_device(
                        self._device_id_input
                    )

                    if not self._http_token:
                        errors["base"] = "auth_failed"
                    else:
                        return await self.async_step_confirm_device()

                except AuthException:
                    _LOGGER.exception(
                        "Authentication failed for device %s",
                        self._device_id_input,
                    )
                    errors["base"] = "auth_failed"

                except ApiException:
                    _LOGGER.exception(
                        "API error while authenticating device %s",
                        self._device_id_input,
                    )
                    errors["base"] = "api_error"

                except Exception:
                    _LOGGER.exception(
                        "Unexpected authentication error for device %s",
                        self._device_id_input,
                    )
                    errors["base"] = "unknown"

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_ID,
                    default=self._device_id_input or "",
                ): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_confirm_device(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> ConfigFlowResult:
        """Confirm the discovered Lumentree device."""
        errors: Dict[str, str] = {}

        if not self._http_token:
            return self.async_abort(reason="token_missing")

        try:
            # Only request device information the first time
            if self._device_sn_from_api is None:
                device_info_api = await self.api.get_device_info(
                    self._device_id_input
                )

                if not device_info_api:
                    errors["base"] = "device_not_found"
                elif isinstance(device_info_api, dict) and device_info_api.get("_error"):
                    _LOGGER.error(
                        "Lumentree device info error: %s",
                        device_info_api.get("_error"),
                    )
                    errors["base"] = "api_error"
                else:
                    # Device SN / ID
                    self._device_sn_from_api = (
                        device_info_api.get("deviceId")
                        or device_info_api.get("device_id")
                        or self._device_id_input
                    )

                    # Device name
                    self._device_name = (
                        device_info_api.get("remarkName")
                        or device_info_api.get("deviceType")
                        or f"Lumentree {self._device_sn_from_api}"
                    )

                    await self.async_set_unique_id(
                        self._device_sn_from_api
                    )

                    # -------------------------------------------------
                    # FIX:
                    # If this device already exists, treat this flow
                    # as re-authentication instead of calling
                    # _abort_if_unique_id_configured().
                    # -------------------------------------------------
                    existing_entry = (
                        self.hass.config_entries.async_entry_for_domain_unique_id(
                            DOMAIN,
                            self._device_sn_from_api,
                        )
                    )

                    if existing_entry is not None:
                        self._reauth_entry = existing_entry

                        _LOGGER.info(
                            "Device %s already configured. "
                            "Using existing entry for re-authentication.",
                            self._device_sn_from_api,
                        )

            if errors:
                schema = vol.Schema({})
                return self.async_show_form(
                    step_id="confirm_device",
                    data_schema=schema,
                    errors=errors,
                )

        except AuthException:
            _LOGGER.exception(
                "Authentication error while confirming device %s",
                self._device_id_input,
            )
            errors["base"] = "auth_failed"

        except ApiException:
            _LOGGER.exception(
                "API error while confirming device %s",
                self._device_id_input,
            )
            errors["base"] = "api_error"

        except AbortFlow:
            # IMPORTANT:
            # Do not convert HA AbortFlow into "unknown".
            raise

        except Exception:
            _LOGGER.exception(
                "Unexpected confirm error %s",
                self._device_id_input,
            )
            errors["base"] = "unknown"

        if errors:
            schema = vol.Schema({})
            return self.async_show_form(
                step_id="confirm_device",
                data_schema=schema,
                errors=errors,
            )

        # User confirmed device
        if user_input is not None:
            config_data = {
                CONF_DEVICE_ID: self._device_id_input,
                CONF_DEVICE_SN: self._device_sn_from_api,
                CONF_DEVICE_NAME: self._device_name,
                CONF_HTTP_TOKEN: self._http_token,
            }

            # ---------------------------------------------------------
            # RE-AUTH EXISTING ENTRY
            # ---------------------------------------------------------
            if self._reauth_entry is not None:
                _LOGGER.info(
                    "Updating existing Lumentree entry for device %s",
                    self._device_sn_from_api,
                )

                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        **config_data,
                    },
                )

                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id
                )

                return self.async_abort(
                    reason="reauth_successful"
                )

            # ---------------------------------------------------------
            # NEW ENTRY
            # ---------------------------------------------------------
            return self.async_create_entry(
                title=self._device_name or self._device_sn_from_api,
                data=config_data,
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_NAME,
                    default=self._device_name or "",
                ): str,
            }
        )

        return self.async_show_form(
            step_id="confirm_device",
            data_schema=schema,
        )

    async def async_step_reauth(
        self, entry_data: Dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication."""
        entry_id = self.context.get("entry_id")

        if not entry_id:
            return self.async_abort(reason="unknown")

        entry = self.hass.config_entries.async_get_entry(entry_id)

        if entry is None:
            return self.async_abort(reason="unknown")

        self._reauth_entry = entry

        self._device_id_input = entry.data.get(CONF_DEVICE_ID)

        if not self._device_id_input:
            return self.async_abort(reason="invalid_device_id")

        # Reset authentication state
        self._http_token = None
        self._device_sn_from_api = None
        self._device_name = None
        self._api = None

        return await self.async_step_user(
            {
                CONF_DEVICE_ID: self._device_id_input,
            }
        )

    async def async_step_reconfigure(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration."""
        entry_id = self.context.get("entry_id")

        if not entry_id:
            return self.async_abort(reason="unknown")

        entry = self.hass.config_entries.async_get_entry(entry_id)

        if entry is None:
            return self.async_abort(reason="unknown")

        self._reauth_entry = entry

        if user_input is None:
            user_input = {
                CONF_DEVICE_ID: entry.data.get(CONF_DEVICE_ID, ""),
            }

        return await self.async_step_user(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ):
        """Return options flow."""
        return LumentreeOptionsFlow(config_entry)


class LumentreeOptionsFlow(config_entries.OptionsFlow):
    """Handle Lumentree options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self,
        user_input: Optional[Dict[str, Any]] = None,
    ) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data=user_input,
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
        )
