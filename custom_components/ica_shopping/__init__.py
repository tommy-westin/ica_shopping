import logging
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers import entity_registry

from .const import DOMAIN, DATA_ICA
from .ica_api import ICAApi

_LOGGER = logging.getLogger(__name__)


async def _trigger_sensor_update(hass, list_id):
    registry = entity_registry.async_get(hass)
    target_unique_id = f"shoppinglist_{list_id}"
    sensor_entity = None

    for entity in registry.entities.values():
        if entity.unique_id == target_unique_id:
            sensor_entity = entity.entity_id
            break

    if not sensor_entity:
        _LOGGER.debug("Kunde inte hitta sensor med unique_id %s", target_unique_id)
        return

    _LOGGER.debug("Triggar update för %s", sensor_entity)
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": sensor_entity},
        blocking=True,
    )


STORAGE_VERSION = 1
STORAGE_KEY = "ica_keep_synced_list"
MAX_ICA_ITEMS = 250
MAX_KEEP_ITEMS = 100
DEBOUNCE_SECONDS = 1


async def async_setup(hass, config):
    return True


async def async_setup_entry(hass, entry):
    _LOGGER.debug("ICA Shopping initieras via UI config entry")

    session_id = entry.options.get("session_id", entry.data["session_id"])
    list_id = entry.options.get("ica_list_id", entry.data["ica_list_id"])

    api = ICAApi(hass, session_id=session_id)
    hass.data.setdefault(DOMAIN, {})[DATA_ICA] = api
    hass.data[DOMAIN]["current_list_id"] = list_id

    keep_entity = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))
    if not keep_entity:
        _LOGGER.warning("Ingen todo-entity vald. Keep-synk inaktiveras, men ICA-sensor och services fungerar.")
        keep_entity = None

    debounce_unsub = None

    async def schedule_sync(_now=None):
        nonlocal debounce_unsub
        debounce_unsub = None

        list_id_local = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))
        keep_entity_local = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))

        _LOGGER.debug("Debounced Keep -> ICA sync")
        try:
            result = await hass.services.async_call(
                "todo",
                "get_items",
                {"entity_id": keep_entity_local},
                blocking=True,
                return_response=True,
            )

            items = result.get(keep_entity_local, {}).get("items", [])
            summaries = [i.get("summary", "").strip() for i in items if isinstance(i, dict)]
            if len(summaries) > MAX_KEEP_ITEMS:
                summaries = summaries[:MAX_KEEP_ITEMS]

            lists = await api.fetch_lists()
            rows = next((l.get("rows", []) for l in lists if l.get("id") == list_id_local), [])
            if len(rows) >= MAX_ICA_ITEMS:
                _LOGGER.error("ICA-listan full (%s). Inga varor tillagda.", len(rows))
                return

            existing = [r.get("text", "").strip().lower() for r in rows if isinstance(r, dict)]
            space = MAX_ICA_ITEMS - len(rows)
            to_add = [s for s in summaries if s.lower() not in existing][:space]

            any_added = False
            for text in to_add:
                success = await api.add_to_list(list_id_local, text)
                if success:
                    _LOGGER.info("Lade till '%s' i ICA", text)
                    any_added = True

            if any_added:
                await _trigger_sensor_update(hass, list_id_local)

        except Exception as e:
            _LOGGER.error("Fel vid sync_keep_to_ica: %s", e)

    def call_service_listener(event):
        nonlocal debounce_unsub

        data = event.data.get("service_data", {})
        service = event.data.get("service")

        if service == "update_item":
            status = data.get("status")
            text = data.get("rename")
            if status == "completed" and text:
                item = text.strip().lower()
                hass.data[DOMAIN].setdefault("recent_keep_removes", set()).add(item)
                _LOGGER.debug("Avlyssnad remove via update_item: %s", item)

                async def remove_from_ica_direct():
                    try:
                        list_id_local = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))
                        lists = await api.fetch_lists()
                        rows = next((l.get("rows", []) for l in lists if l.get("id") == list_id_local), [])
                        ica_rows_dict = {
                            row.get("text", "").strip().lower(): row.get("id")
                            for row in rows if isinstance(row, dict)
                        }
                        row_id = ica_rows_dict.get(item)
                        if row_id:
                            await api.remove_item(row_id)
                            _LOGGER.info("Direkt borttagning av '%s' från ICA (pga completed i Keep)", item)
                    except Exception as e:
                        _LOGGER.error("Fel vid direkt ICA-radering: %s", e)

                hass.async_create_task(remove_from_ica_direct())

                if debounce_unsub:
                    debounce_unsub()
                debounce_unsub = async_call_later(hass, DEBOUNCE_SECONDS, schedule_sync)

        entity_ids = data.get("entity_id", [])
        keep_entity_local = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))

        item = data.get("item")
        if isinstance(item, str):
            item = item.strip().lower()
        elif isinstance(item, list) and item:
            item = item[0].strip().lower()
        else:
            item = None

        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        if keep_entity_local not in entity_ids or not item:
            return

        if service == "add_item":
            hass.data[DOMAIN].setdefault("recent_keep_adds", set()).add(item)
            _LOGGER.debug("Noterat add_item i Keep: %s", item)

        elif service == "remove_item":
            hass.data[DOMAIN].setdefault("recent_keep_removes", set()).add(item)
            _LOGGER.debug("Noterat remove_item i Keep: %s", item)

        if debounce_unsub:
            debounce_unsub()
        debounce_unsub = async_call_later(hass, DEBOUNCE_SECONDS, schedule_sync)

    hass.bus.async_listen("call_service", call_service_listener)

    async def handle_refresh(call):
        _LOGGER.debug("ICA refresh triggered via service")
        try:
            remove_striked = entry.options.get("remove_striked", True)
            keep_entity_local = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))
            list_id_local = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))

            lists = await api.fetch_lists()
            the_list = next((l for l in lists if l.get("id") == list_id_local), None)
            if not the_list:
                _LOGGER.warning("Kunde inte hitta ICA-lista %s", list_id_local)
                return

            rows = the_list.get("rows", [])

            if remove_striked:
                checked_rows = [r for r in rows if r.get("isStriked") is True and r.get("id")]
                for r in checked_rows:
                    await api.remove_item(r["id"])
                    _LOGGER.info("Rensade avbockad vara '%s' från ICA", r.get("text", ""))
                rows = [r for r in rows if r.get("id") not in [cr["id"] for cr in checked_rows]]

            if len(rows) >= MAX_ICA_ITEMS:
                _LOGGER.error("ICA-listan är full (%s varor). Refresh stoppad.", len(rows))
                return

            ica_items = [row.get("text", "").strip() for row in rows if isinstance(row, dict)]
            ica_items_lower = [x.lower() for x in ica_items]
            ica_rows_dict = {
                row.get("text", "").strip().lower(): row.get("id")
                for row in rows if isinstance(row, dict)
            }

            result = await hass.services.async_call(
                "todo",
                "get_items",
                {"entity_id": keep_entity_local},
                blocking=True,
                return_response=True,
            )
            keep_items = result.get(keep_entity_local, {}).get("items", [])
            keep_summaries = [i.get("summary", "").strip() for i in keep_items if isinstance(i, dict)]
            keep_lower = [x.lower() for x in keep_summaries]

            keep_completed = [
                i.get("summary", "").strip().lower()
                for i in keep_items
                if i.get("status") == "completed"
            ]

            if remove_striked:
                for text in keep_completed:
                    await hass.services.async_call(
                        "todo",
                        "remove_item",
                        {"entity_id": keep_entity_local, "item": text},
                    )
                    _LOGGER.info("Tog bort '%s' från Keep (pga status completed + remove_striked)", text)

            for text in keep_completed:
                row_id = ica_rows_dict.get(text)
                if row_id:
                    await api.remove_item(row_id)
                    _LOGGER.info("Tog bort '%s' från ICA (baserat på Keep completed)", text)

            recent_adds = hass.data[DOMAIN].setdefault("recent_keep_adds", set())
            recent_removes = hass.data[DOMAIN].setdefault("recent_keep_removes", set())

            to_add = []
            for item_text in ica_items:
                key = item_text.lower()
                if key not in keep_lower and key not in recent_removes:
                    _LOGGER.debug("Planerar att lägga till i Keep: %s", item_text)
                    to_add.append(item_text)
                else:
                    _LOGGER.debug("Hoppar över '%s' (recent_removes eller redan i Keep)", item_text)

            max_add = MAX_ICA_ITEMS - len(keep_items)
            to_add = to_add[:max_add]

            for item_text in to_add:
                await hass.services.async_call(
                    "todo",
                    "add_item",
                    {"entity_id": keep_entity_local, "item": item_text},
                )
                _LOGGER.info("Lagt till '%s' i Keep", item_text)

            to_remove_from_keep = [
                i.get("summary") for i in keep_items
                if i.get("summary", "").strip().lower() not in ica_items_lower
            ]

            for summary in to_remove_from_keep:
                if summary:
                    await hass.services.async_call(
                        "todo",
                        "remove_item",
                        {"entity_id": keep_entity_local, "item": summary},
                    )
                    _LOGGER.info("Tagit bort '%s' från Keep", summary)

            to_remove_from_ica = [item_text for item_text in recent_removes if item_text in ica_rows_dict]

            for text in to_remove_from_ica:
                row_id = ica_rows_dict.get(text)
                if row_id:
                    await api.remove_item(row_id)
                    _LOGGER.info("Tog bort '%s' från ICA (baserat på Keep-radering)", text)

            await _trigger_sensor_update(hass, list_id_local)

            if "recent_keep_adds" in hass.data[DOMAIN]:
                hass.data[DOMAIN]["recent_keep_adds"].clear()
            if "recent_keep_removes" in hass.data[DOMAIN]:
                hass.data[DOMAIN]["recent_keep_removes"].clear()

        except Exception as e:
            _LOGGER.error("Fel vid refresh: %s", e)

    hass.services.async_register(DOMAIN, "refresh", handle_refresh)

    async def handle_add_item(call):
        api_local = hass.data.get(DOMAIN, {}).get(DATA_ICA)
        if not api_local:
            _LOGGER.error("ICA API saknas i hass.data. add_item kan inte köras.")
            return

        list_id_local = call.data.get("list_id") or hass.data.get(DOMAIN, {}).get("current_list_id")
        text = call.data.get("text")

        if isinstance(text, str):
            text = text.strip()

        if not list_id_local:
            _LOGGER.error("add_item saknar list_id och inget current_list_id finns sparat.")
            return

        if not text:
            _LOGGER.error("add_item saknar text.")
            return

        _LOGGER.debug("add_item: list_id=%s text=%s", list_id_local, text)

        try:
            success = await api_local.add_to_list(list_id_local, text)
            if not success:
                _LOGGER.error("Misslyckades lägga till '%s' i ICA-lista %s", text, list_id_local)
                return

            _LOGGER.info("Lade till '%s' i ICA-lista %s", text, list_id_local)
            await _trigger_sensor_update(hass, list_id_local)

        except Exception as e:
            _LOGGER.error("Fel vid add_item: %s", e)

    hass.services.async_register(DOMAIN, "add_item", handle_add_item)

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    entry.async_on_unload(entry.add_update_listener(_options_update_listener))

    return True


async def _options_update_listener(hass, entry):
    _LOGGER.debug("Optioner har ändrats, laddar om entry")

    prev_list_id = hass.data[DOMAIN].get("current_list_id")
    new_list_id = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))

    if prev_list_id and prev_list_id != new_list_id:
        _LOGGER.warning(
            "List ID changed from %s to %s. Detta kan trigga sync av tidigare Keep-items till ny ICA-lista.",
            prev_list_id,
            new_list_id,
        )

    hass.data[DOMAIN]["current_list_id"] = new_list_id
    await hass.config_entries.async_reload(entry.entry_id)
