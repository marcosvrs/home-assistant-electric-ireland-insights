"""Config flow for Electric Ireland Insights integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import ElectricIrelandAPI
from .const import (
    CONF_DISCOUNT_PERCENTAGE,
    DOMAIN,
    NAME,
    _get_api_lock,
    _redact_id,
    configured_discount_percentage,
    hash_account_id,
)
from .exceptions import AccountNotFound, CannotConnect, InvalidAuth
from .types import MeterIds

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("username"): str,
        vol.Required("password"): str,
    }
)

type CredentialValidationError = Literal["invalid_auth", "cannot_connect", "account_not_found"]


@dataclass(frozen=True)
class CredentialValidationResult:
    """Normalized outcome from checking an account's credentials."""

    meter_ids: MeterIds | None
    error: CredentialValidationError | None


async def _async_validate_account_credentials(
    hass: HomeAssistant,
    username: str,
    password: str,
    account_number: str,
    *,
    flow_name: str,
) -> CredentialValidationResult:
    """Validate credentials and normalize provider errors for config flows."""
    try:
        async with (
            _get_api_lock(),
            async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar()) as session,
        ):
            api = ElectricIrelandAPI(username, password, account_number)
            meter_ids = await api.validate_credentials(session)
    except InvalidAuth:
        return CredentialValidationResult(None, "invalid_auth")
    except CannotConnect:
        return CredentialValidationResult(None, "cannot_connect")
    except AccountNotFound:
        return CredentialValidationResult(None, "account_not_found")
    except Exception:
        _LOGGER.exception("Unexpected exception during %s", flow_name)
        return CredentialValidationResult(None, "cannot_connect")
    return CredentialValidationResult(meter_ids, None)


class ElectricIrelandInsightsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return ElectricIrelandInsightsOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                async with (
                    _get_api_lock(),
                    async_create_clientsession(self.hass, cookie_jar=aiohttp.CookieJar()) as session,
                ):
                    api = ElectricIrelandAPI(
                        user_input["username"],
                        user_input["password"],
                    )
                    accounts = await api.discover_accounts(session)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except AccountNotFound:
                errors["base"] = "account_not_found"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "cannot_connect"
            else:
                self._username = user_input["username"]
                self._password = user_input["password"]
                self._accounts = accounts
                _LOGGER.debug("Credential validation successful, found %d account(s)", len(accounts))

                if len(accounts) == 1:
                    return await self._finish_flow(accounts[0]["account_number"])

                return await self.async_step_account()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_account(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            account_number = user_input.get("account_number")
            if not account_number:
                errors["base"] = "account_not_found"
            else:
                return await self._finish_flow(account_number)

        accounts = self._accounts
        schema = vol.Schema(
            {
                vol.Required("account_number"): vol.In(
                    {acc["account_number"]: acc["display_name"] for acc in accounts}
                ),
            }
        )

        return self.async_show_form(
            step_id="account",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "num_accounts": str(len(accounts)),
            },
        )

    async def _finish_flow(self, account_number: str) -> ConfigFlowResult:
        result = await _async_validate_account_credentials(
            self.hass,
            self._username,
            self._password,
            account_number,
            flow_name="account setup",
        )
        if result.error is not None:
            return self.async_abort(reason=result.error)

        assert result.meter_ids is not None
        self._account_number = account_number
        self._meter_ids = result.meter_ids
        _LOGGER.debug("Account %s validated, proceeding to options", _redact_id(account_number))
        return await self.async_step_options()

    async def async_step_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(hash_account_id(self._account_number))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"{NAME} ({hash_account_id(self._account_number)})",
                data={
                    "username": self._username,
                    "password": self._password,
                    "account_number": self._account_number,
                    "partner_id": self._meter_ids.get("partner"),
                    "contract_id": self._meter_ids.get("contract"),
                    "premise_id": self._meter_ids.get("premise"),
                    "import_full_history": user_input.get("import_full_history", False),
                },
                options={
                    CONF_DISCOUNT_PERCENTAGE: configured_discount_percentage(
                        user_input.get(CONF_DISCOUNT_PERCENTAGE),
                        None,
                    ),
                },
            )

        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema(
                {
                    vol.Optional("import_full_history", default=True): bool,
                    # No default for the discount: the frontend renders the optional
                    # field unchecked, and an unset value falls back to 0 below.
                    vol.Optional(CONF_DISCOUNT_PERCENTAGE): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                }
            ),
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            new_data = {**reauth_entry.data, "password": user_input["password"]}
            result = await _async_validate_account_credentials(
                self.hass,
                new_data["username"],
                new_data["password"],
                new_data["account_number"],
                flow_name="reauth",
            )
            if result.error is not None:
                errors["base"] = result.error
            else:
                assert result.meter_ids is not None
                await self.async_set_unique_id(hash_account_id(new_data["account_number"]))
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={
                        **new_data,
                        "partner_id": result.meter_ids.get("partner"),
                        "contract_id": result.meter_ids.get("contract"),
                        "premise_id": result.meter_ids.get("premise"),
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("password"): str}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            username = entry.data["username"]
            password = user_input["password"]
            force_rediscovery = user_input.get("force_rediscovery", False)

            result = await _async_validate_account_credentials(
                self.hass,
                username,
                password,
                entry.data["account_number"],
                flow_name="reconfigure",
            )
            if result.error is not None:
                errors["base"] = result.error
            else:
                assert result.meter_ids is not None
                password_changed = password != entry.data["password"]
                import_full_history = user_input.get("import_full_history", False)
                if force_rediscovery or password_changed:
                    new_data = {
                        **entry.data,
                        "password": password,
                        "partner_id": None,
                        "contract_id": None,
                        "premise_id": None,
                        "import_full_history": import_full_history,
                    }
                else:
                    new_data = {
                        **entry.data,
                        "password": password,
                        "partner_id": result.meter_ids.get("partner"),
                        "contract_id": result.meter_ids.get("contract"),
                        "premise_id": result.meter_ids.get("premise"),
                        "import_full_history": import_full_history,
                    }
                return self.async_update_reload_and_abort(entry, data=new_data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required("password"): str,
                        vol.Optional("force_rediscovery", default=False): bool,
                        vol.Optional("import_full_history", default=False): bool,
                    }
                ),
                user_input or {"password": entry.data["password"]},
            ),
            description_placeholders={"username": entry.data["username"]},
            errors=errors,
        )


class ElectricIrelandInsightsOptionsFlow(config_entries.OptionsFlowWithReload):
    """Options flow for Electric Ireland Insights."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        configured_discount = configured_discount_percentage(
            self.config_entry.options.get(CONF_DISCOUNT_PERCENTAGE),
            self.config_entry.data.get(CONF_DISCOUNT_PERCENTAGE),
        )

        if user_input is not None:
            discount = user_input.get(CONF_DISCOUNT_PERCENTAGE)
            if discount is None:
                discount = configured_discount
            options = {
                CONF_DISCOUNT_PERCENTAGE: int(discount),
            }
            return self.async_create_entry(
                title="",
                data=options,
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Optional(CONF_DISCOUNT_PERCENTAGE, default=configured_discount): vol.All(
                            vol.Coerce(int), vol.Range(min=0, max=100)
                        ),
                    }
                ),
                {
                    CONF_DISCOUNT_PERCENTAGE: configured_discount,
                },
            ),
        )
