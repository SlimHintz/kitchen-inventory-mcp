from __future__ import annotations

import hmac
from collections.abc import Callable

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config, oauth
from .auth import BearerAuthMiddleware, log_auth_event
from .db import new_session
from .errors import KitchenError
from .schemas import (
    AddInventoryItemsInput,
    DeleteRecipeInput,
    DismissGrocerySuggestionInput,
    GetInventoryInput,
    GetItemHistoryInput,
    GetPlanningContextInput,
    GetRecipeHistoryInput,
    LogMealInput,
    RateRecipeInput,
    RemoveInventoryItemInput,
    RemovePreferenceInput,
    ResolveDuplicateInput,
    ResolveOrderBatchInput,
    SaveRecipeInput,
    ScoreRecipeCoverageInput,
    SetPreferenceInput,
    SuggestGroceriesInput,
    SweepExpiredInput,
    UndoMealLogInput,
    UpdateInventoryItemInput,
    UpdateRecipeInput,
)
from .tools import inventory as inv
from .tools import planning, recipes
from .tools import preferences as prefs

VERSION = "0.1.0"

mcp = FastMCP("kitchen-inventory")


def _run(fn: Callable, *args) -> dict:
    session = new_session()
    try:
        return fn(session, *args)
    except KitchenError as e:
        session.rollback()
        return e.to_dict()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- Inventory -------------------------------------------------------------


@mcp.tool
def add_inventory_items(payload: AddInventoryItemsInput) -> dict:
    """Add items to inventory from a photo read or a pasted grocery order.
    Before calling, collapse any line items that clearly refer to the same
    physical purchase (identical name, quantity, unit, location, and
    expiration/container metadata) into one entry with the combined count.
    Only submit separate entries for genuinely distinct physical items."""
    return _run(inv.add_inventory_items, payload)


@mcp.tool
def resolve_duplicate(payload: ResolveDuplicateInput) -> dict:
    """Resolve a single pantry_item flagged possible_duplicate (fuzzy name
    match or physical-stock duplicate)."""
    return _run(inv.resolve_duplicate, payload)


@mcp.tool
def resolve_order_batch(payload: ResolveOrderBatchInput) -> dict:
    """Resolve an entire re-pasted-order flag at once via order_batch_id,
    instead of resolving each of its rows individually."""
    return _run(inv.resolve_order_batch, payload)


@mcp.tool
def get_inventory(payload: GetInventoryInput) -> dict:
    """Full inventory, grouped by location, with an expiring_soon block."""
    return _run(inv.get_inventory, payload)


@mcp.tool
def update_inventory_item(payload: UpdateInventoryItemInput) -> dict:
    """Manual correction of quantity, state, location, expiration, category."""
    return _run(inv.update_inventory_item, payload)


@mcp.tool
def remove_inventory_item(payload: RemoveInventoryItemInput) -> dict:
    """Soft-delete a pantry item (used_up / expired / thrown_out)."""
    return _run(inv.remove_inventory_item, payload)


@mcp.tool
def get_item_history(payload: GetItemHistoryInput) -> dict:
    """Append-only history for a pantry item, or across all items since a date."""
    return _run(inv.get_item_history, payload)


@mcp.tool
def sweep_expired(payload: SweepExpiredInput) -> dict:
    """Find everything past its expiration_date. Defaults to dry_run=True."""
    return _run(inv.sweep_expired, payload)


# --- Planning ----------------------------------------------------------------


@mcp.tool
def get_planning_context(payload: GetPlanningContextInput) -> dict:
    """Everything needed to invent meals in one call: inventory, expiring
    items, preferences, a capped set of saved recipes, and what's been
    cooked in the last 14 days."""
    return _run(planning.get_planning_context, payload)


@mcp.tool
def score_recipe_coverage(payload: ScoreRecipeCoverageInput) -> dict:
    """Grade a candidate ingredient list against current inventory."""
    return _run(planning.score_recipe_coverage, payload)


@mcp.tool
def suggest_groceries(payload: SuggestGroceriesInput) -> dict:
    """Rank candidate purchases by what they unlock."""
    return _run(planning.suggest_groceries, payload)


@mcp.tool
def dismiss_grocery_suggestion(payload: DismissGrocerySuggestionInput) -> dict:
    """Dismiss a grocery suggestion so it stops being offered."""
    return _run(planning.dismiss_grocery_suggestion, payload)


# --- Recipes and meals ---------------------------------------------------


@mcp.tool
def save_recipe(payload: SaveRecipeInput) -> dict:
    """Save a new recipe with its ingredients. Ingredients are matched to known items;
    returns the new recipe_id and any ingredients that could not be matched."""
    return _run(recipes.save_recipe, payload)


@mcp.tool
def update_recipe(payload: UpdateRecipeInput) -> dict:
    """Edit a saved recipe, including renaming it (name is just a field)."""
    return _run(recipes.update_recipe, payload)


@mcp.tool
def delete_recipe(payload: DeleteRecipeInput) -> dict:
    """Soft-delete (archive) a saved recipe. meal_log/ratings history is preserved."""
    return _run(recipes.delete_recipe, payload)


@mcp.tool
def rate_recipe(payload: RateRecipeInput) -> dict:
    """Rate a recipe for one household member. Re-rating replaces that member's earlier rating."""
    return _run(recipes.rate_recipe, payload)


@mcp.tool
def log_meal(payload: LogMealInput) -> dict:
    """Deplete inventory for a cooked meal. Aborts with a shortfalls list if
    depletion would take anything negative, unless force=true."""
    return _run(recipes.log_meal, payload)


@mcp.tool
def undo_meal_log(payload: UndoMealLogInput) -> dict:
    """Undo a logged meal and restore the inventory it depleted."""
    return _run(recipes.undo_meal_log, payload)


@mcp.tool
def get_recipe_history(payload: GetRecipeHistoryInput) -> dict:
    """Saved recipes with ratings per member, average rating, cook count and last cooked date.
    Filter by tag, minimum rating, or cooked-since date."""
    return _run(recipes.get_recipe_history, payload)


# --- Preferences -----------------------------------------------------------


@mcp.tool
def get_preferences() -> dict:
    """All household food preferences (likes, dislikes, allergies and so on), and whom each applies to."""
    return _run(prefs.get_preferences)


@mcp.tool
def set_preference(payload: SetPreferenceInput) -> dict:
    """Add or update a household food preference (kind + subject + who it applies to)."""
    return _run(prefs.set_preference, payload)


@mcp.tool
def remove_preference(payload: RemovePreferenceInput) -> dict:
    """Delete a household food preference by id."""
    return _run(prefs.remove_preference, payload)


# --- HTTP plumbing -----------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": VERSION})


# --- Minimal OAuth wrapper (see oauth.py docstring for why this exists) -----


def _server_misconfigured() -> JSONResponse:
    return JSONResponse(
        {"error": "server_error", "error_description": "KITCHEN_MCP_PUBLIC_URL is not configured"},
        status_code=500,
    )


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"], include_in_schema=False)
async def oauth_authorization_server_metadata(request: Request) -> JSONResponse:
    base = config.public_url()
    if not base:
        return _server_misconfigured()
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
    )


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"], include_in_schema=False)
async def oauth_protected_resource_metadata(request: Request) -> JSONResponse:
    base = config.public_url()
    if not base:
        return _server_misconfigured()
    return JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})


@mcp.custom_route("/oauth/register", methods=["POST"], include_in_schema=False)
async def oauth_register(request: Request) -> JSONResponse:
    body = await request.json()
    redirect_uris = body.get("redirect_uris") or []
    if not redirect_uris:
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "redirect_uris is required"},
            status_code=400,
        )
    session = new_session()
    try:
        result = oauth.register_client(session, body.get("client_name"), redirect_uris)
    finally:
        session.close()
    return JSONResponse(result, status_code=201)


@mcp.custom_route("/oauth/authorize", methods=["GET", "POST"], include_in_schema=False)
async def oauth_authorize(request: Request):
    if request.method == "GET":
        params = request.query_params
    else:
        params = await request.form()

    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "S256")

    session = new_session()
    try:
        client = oauth.get_client(session, client_id)
        if client is None or redirect_uri not in (client.redirect_uris or []):
            return HTMLResponse("Unknown client or redirect_uri.", status_code=400)

        hidden_fields = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        }

        if request.method == "GET":
            return HTMLResponse(
                oauth.render_authorize_form(
                    client_name=client.client_name,
                    redirect_uri=redirect_uri,
                    hidden_fields=hidden_fields,
                    error=None,
                )
            )

        token = params.get("token", "")
        expected = config.auth_token()
        if not expected or not hmac.compare_digest(token, expected):
            client_ip = request.client.host if request.client else "unknown"
            log_auth_event("oauth authorize failure source_ip=%s", client_ip)
            return HTMLResponse(
                oauth.render_authorize_form(
                    client_name=client.client_name,
                    redirect_uri=redirect_uri,
                    hidden_fields=hidden_fields,
                    error="Incorrect token, try again.",
                ),
                status_code=401,
            )

        code = oauth.issue_authorization_code(
            session, client_id, redirect_uri, code_challenge, code_challenge_method
        )
        separator = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{separator}code={code}&state={state}", status_code=302)
    finally:
        session.close()


@mcp.custom_route("/oauth/token", methods=["POST"], include_in_schema=False)
async def oauth_token(request: Request) -> JSONResponse:
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    session = new_session()
    try:
        access_token, error = oauth.exchange_code_for_token(
            session,
            code=form.get("code", ""),
            client_id=form.get("client_id", ""),
            redirect_uri=form.get("redirect_uri", ""),
            code_verifier=form.get("code_verifier", ""),
        )
    finally:
        session.close()

    if error:
        return JSONResponse({"error": error}, status_code=400)
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(oauth.ACCESS_TOKEN_TTL.total_seconds()),
        }
    )


def build_app():
    return mcp.http_app(transport="streamable-http", middleware=[Middleware(BearerAuthMiddleware)])
