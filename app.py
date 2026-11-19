"""
Monday.com webhook: Labor Planning subitem set to YES -> create item on Crewing
AND connect it to the Events N. America item (which the native automation won't do).

Board chain: Events N. America  <-(connect col)-  Labor Planning item
                                                    └─ subitem (YES trigger)
             Crewing item (new)  -(connect col)->  Events N. America item

Flow:
  1. Webhook fires on "change_subitem_column_value" (Labor Planning board).
  2. If the trigger column is set to YES, read the subitem -> its parent
     Labor Planning item -> the Events N. America item that parent links to.
  3. Skip if a Crewing item already exists for this subitem (dedupe).
  4. Create the Crewing item (named after the subitem) with Event #,
     source subitem ID, and "link to Events N. America" already populated.

Env vars: MONDAY_TOKEN
Deploy: same as the UK one (Flask on Render). Use /discover to fill in the IDs below.
"""
import json
import os

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

API_URL = "https://api.monday.com/v2"
HEADERS = {
    "Authorization": os.environ["MONDAY_TOKEN"],
    "Content-Type": "application/json",
    "API-Version": "2024-10",
}

# ---- Fill these in from /discover ----------------------------------------
LABOR_BOARD_ID = 18337321246   # Labor Planning board (where the subitems live)
CREWING_BOARD_ID = 18397508571 # Crew Services - Crewing
EVENTS_BOARD_ID = 269905960    # Global Events (formerly Events N. America)
CREWING_GROUP_ID = "group_title"   # "Offers"

# Labor Planning subitems board ("Subitems of Global Labor Planning Dashboard")
LABOR_SUBITEMS_BOARD_ID = 18381758369
SUB_TRIGGER_COL = "color_mkxva8bh"          # "Ready for Offer Y/N"
SUB_TRIGGER_LABEL = "Yes"                   # must match the label text exactly
SUB_CREWING_LINK_COL = "board_relation_mm01p6ez"  # subitem "Crew Services - Staffing" -> Crewing

# Labor Planning parent item: its connect column pointing at Global Events
LABOR_EVENTS_LINK_COL = "board_relation_mkxnyqdm"  # "link to Events N. America"

# Global Events columns
EVENT_NUMBER_COL = "text"      # Event # on Global Events (used for logging only)

# Crewing board columns (confirmed from /discover)
CREW_LINK_COL = "board_relation_mkxnyqdm"        # "link to Events N. America"
CREW_LABOR_LINK_COL = "board_relation_mm98ec8m"  # "link to Global Labor Planning Dashboard"
CREW_SOURCE_SUBITEM_COL = "text_mm98xv5k"        # "Source Subitem ID" (dedupe)
# Note: Crewing "Event #" is a mirror column - it fills itself from the event link.
# ---------------------------------------------------------------------------


def gql(query, variables=None):
    r = requests.post(API_URL, headers=HEADERS,
                      json={"query": query, "variables": variables or {}}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def col_text(item, col_id):
    for c in item.get("column_values", []):
        if c["id"] == col_id:
            return c.get("text") or ""
    return ""


def get_subitem_chain(subitem_id):
    """subitem -> parent Labor Planning item -> linked Events N. America item(s)."""
    q = """
    query ($ids: [ID!], $linkCol: [String!], $eventCols: [String!]) {
      items(ids: $ids) {
        id
        name
        parent_item {
          id
          name
          column_values(ids: $linkCol) {
            id
            ... on BoardRelationValue {
              linked_item_ids
              linked_items {
                id
                name
                column_values(ids: $eventCols) { id text }
              }
            }
          }
        }
      }
    }"""
    data = gql(q, {
        "ids": [str(subitem_id)],
        "linkCol": [LABOR_EVENTS_LINK_COL],
        "eventCols": [EVENT_NUMBER_COL],
    })
    items = data["items"]
    return items[0] if items else None


def already_created(subitem_id):
    q = """
    query ($board: ID!, $col: String!, $val: String!) {
      items_page_by_column_values(
        board_id: $board, limit: 1,
        columns: [{column_id: $col, column_values: [$val]}]
      ) { items { id } }
    }"""
    data = gql(q, {"board": str(CREWING_BOARD_ID),
                   "col": CREW_SOURCE_SUBITEM_COL, "val": str(subitem_id)})
    return bool(data["items_page_by_column_values"]["items"])


def create_linked_item(name, event_item_ids, labor_item_id, subitem_id):
    column_values = {
        CREW_SOURCE_SUBITEM_COL: str(subitem_id),
        CREW_LABOR_LINK_COL: {"item_ids": [int(labor_item_id)]},
    }
    if event_item_ids:
        # Crewing's event link only allows one item
        column_values[CREW_LINK_COL] = {"item_ids": [int(event_item_ids[0])]}  # <-- the connection
    q = """
    mutation ($board: ID!, $group: String!, $name: String!, $cols: JSON!) {
      create_item(board_id: $board, group_id: $group, item_name: $name,
                  column_values: $cols, create_labels_if_missing: true) { id }
    }"""
    data = gql(q, {"board": str(CREWING_BOARD_ID), "group": CREWING_GROUP_ID,
                   "name": name, "cols": json.dumps(column_values)})
    return data["create_item"]["id"]


def link_subitem_to_crewing(subitem_id, crewing_item_id):
    """Point the Labor Planning subitem's 'Crew Services - Staffing' column at the
    new Crewing item, so its Rate Negotiation / Travel / Onboarded mirrors work."""
    q = """
    mutation ($board: ID!, $item: ID!, $cols: JSON!) {
      change_multiple_column_values(board_id: $board, item_id: $item,
                                    column_values: $cols) { id }
    }"""
    gql(q, {"board": str(LABOR_SUBITEMS_BOARD_ID), "item": str(subitem_id),
            "cols": json.dumps({SUB_CREWING_LINK_COL: {"item_ids": [int(crewing_item_id)]}})})


@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(force=True, silent=True) or {}

    # Monday's one-time URL verification
    if "challenge" in body:
        return jsonify({"challenge": body["challenge"]})

    event = body.get("event", {})
    if event.get("columnId") != SUB_TRIGGER_COL:
        return jsonify(ok=True, skipped="other column")

    label = ((event.get("value") or {}).get("label") or {}).get("text")
    if label != SUB_TRIGGER_LABEL:
        return jsonify(ok=True, skipped=f"label {label!r}")

    subitem_id = event.get("pulseId")
    try:
        if already_created(subitem_id):
            return jsonify(ok=True, skipped="duplicate")

        sub = get_subitem_chain(subitem_id)
        if not sub or not sub.get("parent_item"):
            return jsonify(ok=False, error="no parent Labor Planning item"), 200

        parent = sub["parent_item"]
        link = next((c for c in parent["column_values"]
                     if c["id"] == LABOR_EVENTS_LINK_COL), {})
        events = link.get("linked_items") or []
        if not events:
            # Still create the item so nobody is lost; it just won't be linked.
            app.logger.warning("Labor item %s has no Events N. America link", parent["id"])

        event_ids = [e["id"] for e in events]
        event_number = col_text(events[0], EVENT_NUMBER_COL) if events else ""

        new_id = create_linked_item(sub["name"], event_ids, parent["id"], subitem_id)
        try:
            link_subitem_to_crewing(subitem_id, new_id)
        except Exception:
            app.logger.exception("Created %s but couldn't link subitem back", new_id)
        app.logger.info("Created %s linked to event %s (Event # %s)",
                        new_id, event_ids, event_number)
        return jsonify(ok=True, created=new_id, linked_events=event_ids)
    except Exception as e:  # return 200 so monday doesn't retry-spam
        app.logger.exception("webhook failed")
        return jsonify(ok=False, error=str(e)), 200


@app.route("/discover", methods=["GET"])
def discover():
    """GET /discover?board=<id> -> groups + columns (with types) for that board,
    plus the subitems board's columns if it has one."""
    board_id = request.args.get("board", str(LABOR_BOARD_ID))
    q = """
    query ($ids: [ID!]) {
      boards(ids: $ids) {
        id name
        groups { id title }
        columns { id title type settings_str }
      }
    }"""
    board = gql(q, {"ids": [board_id]})["boards"][0]
    out = {"board": board}
    for c in board["columns"]:
        if c["type"] == "subtasks":
            sub_ids = json.loads(c["settings_str"]).get("boardIds", [])
            if sub_ids:
                out["subitems_board"] = gql(q, {"ids": [str(sub_ids[0])]})["boards"][0]
    return jsonify(out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
