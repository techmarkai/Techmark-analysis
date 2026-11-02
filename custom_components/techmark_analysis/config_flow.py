"""Config and options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.httpx_client import get_async_client

from .const import (
    CONF_CUSTOMER_FDID,
    CONF_CUSTOMER_WINDOW,
    CONF_EMPLOYEE_FDID,
    CONF_EMPLOYEE_WINDOW,
    CONF_ENROLL,
    CONF_ENROLL_MIN_FACE_PX,
    CONF_ENROLL_MIN_SCORE,
    CONF_MIN_FACE_QUALITY,
    CONF_MODEL,
    CONF_REPORT_TIME,
    CONF_RETENTION_DAYS,
    CONF_SERIAL,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from .isapi import AuthError, CameraError, HikClient

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME, default="admin"): str,
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


async def _probe(hass: HomeAssistant, host: str, user: str, password: str) -> tuple[dict | None, list, str | None]:
    client = HikClient(get_async_client(hass), host, user, password)
    try:
        info = await client.device_info()
        libs = await client.list_libraries()
    except AuthError:
        return None, [], "invalid_auth"
    except CameraError:
        return None, [], "cannot_connect"
    return info, libs, None


def _detect(libs: list[dict], word: str, default: str) -> str:
    for lib in libs:
        if word in lib["name"].lower():
            return lib["fdid"]
    return default


class TechmarkConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            info, libs, err = await _probe(
                self.hass, user_input[CONF_HOST], user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if err:
                errors["base"] = err
            else:
                await self.async_set_unique_id(info["serialNumber"])
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                options = {
                    **DEFAULT_OPTIONS,
                    CONF_EMPLOYEE_FDID: _detect(libs, "employee", DEFAULT_OPTIONS[CONF_EMPLOYEE_FDID]),
                    CONF_CUSTOMER_FDID: _detect(libs, "customer", DEFAULT_OPTIONS[CONF_CUSTOMER_FDID]),
                }
                return self.async_create_entry(
                    title=f"Techmark {info['model']}",
                    data={**user_input, CONF_SERIAL: info["serialNumber"], CONF_MODEL: info["model"]},
                    options=options,
                )
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            _, _, err = await _probe(
                self.hass, entry.data[CONF_HOST], user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if err:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TechmarkOptionsFlow()


def _int(lo: int, hi: int):
    return selector.NumberSelector(selector.NumberSelectorConfig(min=lo, max=hi, step=1, mode=selector.NumberSelectorMode.BOX))


class TechmarkOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for key in (
                CONF_MIN_FACE_QUALITY, CONF_EMPLOYEE_WINDOW, CONF_CUSTOMER_WINDOW, CONF_RETENTION_DAYS,
                CONF_ENROLL_MIN_SCORE, CONF_ENROLL_MIN_FACE_PX,
            ):
                user_input[key] = int(user_input[key])
            return self.async_create_entry(data=user_input)
        o = {**DEFAULT_OPTIONS, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(CONF_MIN_FACE_QUALITY, default=o[CONF_MIN_FACE_QUALITY]): _int(0, 100),
                vol.Required(CONF_EMPLOYEE_WINDOW, default=o[CONF_EMPLOYEE_WINDOW]): _int(5, 240),
                vol.Required(CONF_CUSTOMER_WINDOW, default=o[CONF_CUSTOMER_WINDOW]): _int(5, 240),
                vol.Required(CONF_EMPLOYEE_FDID, default=o[CONF_EMPLOYEE_FDID]): str,
                vol.Required(CONF_CUSTOMER_FDID, default=o[CONF_CUSTOMER_FDID]): str,
                vol.Required(CONF_REPORT_TIME, default=o[CONF_REPORT_TIME]): selector.TimeSelector(),
                vol.Required(CONF_RETENTION_DAYS, default=o[CONF_RETENTION_DAYS]): _int(30, 3650),
                vol.Required(CONF_ENROLL, default=o[CONF_ENROLL]): bool,
                vol.Required(CONF_ENROLL_MIN_SCORE, default=o[CONF_ENROLL_MIN_SCORE]): _int(50, 100),
                vol.Required(CONF_ENROLL_MIN_FACE_PX, default=o[CONF_ENROLL_MIN_FACE_PX]): _int(40, 1000),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
