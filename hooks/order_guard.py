#!/usr/bin/env python3
"""Order guard: a Claude Code hook that stops any automatic order. Standard library only.

The skills in this pack never place orders: the user types every order into
their own broker. This hook makes that a rule Claude cannot talk its way
around, whatever a skill, a web page or a headline says. It runs before every
MCP tool call and every Bash command (PreToolUse), and after MCP calls
(PostToolUse) to notice when a broker page is open.

It DENIES:
  1. Order tools. Any MCP tool whose name places, changes, cancels or closes
     an order or a position: a name with order / position / boleta in it
     (place_stock_order, submit_order, cancel_order, close_all_positions),
     a trading verb (trade, flatten, liquidate, exercise), or buy / sell of
     a security (buy_stock, sell_crypto, buy). Read-only tools (get_, list_,
     read_, search_, fetch_, describe_ ...) pass, and so does everything else
     (a calendar's create_event is not an order).
  2. Browser input on a broker. Once a broker page has been opened or seen
     in this session (XP and the other Brazilian brokers below, or any domain
     in B3_BROKER_DOMAINS), the browser and desktop-control tools go
     READ-ONLY for the rest of the session: no clicks, typing, key presses,
     drags, form filling, JavaScript, uploads or batches. Navigating to a
     page, reading it, finding text, scrolling, zooming and screenshots still
     work, so Claude can read live prices while the user does all the
     clicking. Logging in is the user's job, in their own browser.
  3. Bash talking to a broker. A shell command that names a broker domain
     AND a network tool or library (curl to a broker API with a copied
     cookie, a python -c with urllib, ...). Text that only mentions a broker
     (writing these docs) passes.

It never asks and has no off switch: to trade, the user uses their broker.
State lives in <tmp>/b3-trader-guard/<session id>.json (just "a broker page
was seen"). If the hook itself fails on a tool that looks like an order tool,
it denies (fail closed).

Install: as a plugin, hooks/hooks.json wires it in; in this repo,
.claude/settings.json does. Copying only the skill folders leaves it out.
"""

import json
import os
import re
import sys
import tempfile

BROKER_DOMAINS = (
    "xpi.com.br", "xpinc.com", "xpinvestimentos.com.br", "xp.com.br", "rico.com.vc", "clear.com.br",
    "btgpactual.com", "nuinvest.com.br", "toroinvestimentos.com.br", "genialinvestimentos.com.br",
    "modalmais.com.br", "agorainvestimentos.com.br", "inter.co", "bancointer.com.br", "avenue.us",
    "orama.com.br", "warren.com.br", "mirae.com.br", "nelogica.com.br", "profitchart.com.br",
)
READ_PREFIXES = ("get_", "list_", "read_", "search_", "fetch_", "describe_", "query_", "show_", "view_",
                 "lookup_", "find_", "check_")
ORDER_NOUNS = re.compile(r"order|position|boleta|ordem", re.I)
TRADE_VERBS = re.compile(r"(^|_)(trade|trading|flatten|liquidat\w*|exercise|short_sell)(_|$)", re.I)
BUY_SELL = re.compile(r"(^|_)(buy|sell|comprar|vender)(_|$)", re.I)
SECURITY_WORDS = re.compile(r"stock|share|equity|equities|crypto|option|asset|security|securities|ticker|"
                            r"acao|acoes|ativo|papel|etf|fund|bond", re.I)
BROWSER = re.compile(r"^mcp__(claude-in-chrome|Claude_Browser|remote-devices__Claude_Browser|"
                     r"computer-use|remote-devices__computer)", re.I)
BROWSER_INPUT_TOOLS = {"form_input", "javascript_tool", "file_upload", "upload_image", "browser_batch",
                       "left_click", "right_click", "double_click", "triple_click", "type", "key",
                       "left_click_drag", "drag", "paste", "write_clipboard", "open_application"}
SAFE_ACTIONS = {"screenshot", "scroll", "scroll_up", "scroll_down", "zoom", "wait", "mouse_move", "hover",
                "cursor_position"}
NAV_TOOLS = {"navigate", "tabs_create_mcp", "tabs_create", "preview_start"}
NETWORK = re.compile(r"\b(curl|wget|httpie|xh|nc|ncat|telnet|openssl|requests|urllib|httpx|aiohttp|"
                     r"fetch|Invoke-WebRequest|iwr|websocat|socket)\b", re.I)


def domains():
    extra = [d.strip().lower() for d in os.environ.get("B3_BROKER_DOMAINS", "").split(",") if d.strip()]
    return tuple(BROKER_DOMAINS) + tuple(extra)


def mentions_broker(text):
    t = str(text or "").lower()
    return any(re.search(r"(^|[^a-z0-9-])" + re.escape(d) + r"($|[^a-z0-9-])", t) or ("." + d) in t
               for d in domains())


def state_path(session):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(session or "default"))[:80]
    d = os.path.join(tempfile.gettempdir(), "b3-trader-guard")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, safe + ".json")


def broker_seen(session):
    try:
        with open(state_path(session), encoding="utf-8") as fh:
            return bool(json.load(fh).get("broker_seen"))
    except (OSError, ValueError):
        return False


def mark_broker(session):
    p = state_path(session)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"broker_seen": True}, fh)
    os.replace(tmp, p)


def short_name(tool):
    return tool.split("__")[-1] if tool.startswith("mcp__") else tool


def is_order_tool(tool):
    if not tool.startswith("mcp__") or BROWSER.match(tool):
        return False
    name = short_name(tool).lower()
    if name.startswith(READ_PREFIXES):
        return False
    if ORDER_NOUNS.search(name) or TRADE_VERBS.search(name):
        return True
    if BUY_SELL.search(name):
        return name in ("buy", "sell") or bool(SECURITY_WORDS.search(name))
    return False


def browser_input(tool, tool_input):
    """Is this browser / desktop call something that clicks, types or runs code?"""
    name = short_name(tool)
    if name in BROWSER_INPUT_TOOLS:
        return True
    action = str((tool_input or {}).get("action") or "").lower()
    if name in ("computer", "computer_use") or action:
        return bool(action) and action not in SAFE_ACTIONS
    return False


def decide(event, payload):
    """(decision, reason): decision is "deny" or None."""
    tool = str(payload.get("tool_name") or "")
    tin = payload.get("tool_input") or {}
    session = payload.get("session_id")
    if event == "post":
        if BROWSER.match(tool) and (mentions_broker(json.dumps(tin)) or
                                    mentions_broker(json.dumps(payload.get("tool_response"))[:200000])):
            mark_broker(session)
        return None, None
    if tool == "Bash":
        cmd = str(tin.get("command") or "")
        if mentions_broker(cmd) and NETWORK.search(cmd):
            return "deny", ("order guard: shell commands that talk to a broker are blocked. Read prices in the "
                            "browser (read-only) and record them with alerts.py set-price.")
        return None, None
    if is_order_tool(tool):
        return "deny", ("order guard: %s can place or change an order or a position. This pack never sends "
                        "orders: show the user the ticket (trader-broker) and let them type it into their broker."
                        % tool)
    if BROWSER.match(tool):
        name = short_name(tool)
        if name in NAV_TOOLS and mentions_broker(json.dumps(tin)):
            mark_broker(session)          # navigating is fine; from now on the browser is read-only
            return None, None
        if browser_input(tool, tin) and (broker_seen(session) or mentions_broker(json.dumps(tin))):
            return "deny", ("order guard: a broker page is open in this session, so the browser is read-only: "
                            "no clicks, typing, forms or scripts. Read the page (get_page_text, read_page, "
                            "screenshot, scroll) and ask the user to do anything that needs a click.")
    return None, None


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "pre"
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
        decision, reason = decide(event, payload)
    except Exception as e:          # fail closed on anything that looks like an order
        decision, reason = None, None
        if event == "pre" and re.search(r"order|position|trade|buy|sell", raw, re.I) and '"mcp__' in raw:
            decision, reason = "deny", "order guard: could not check this call (%s): blocked to be safe" % e
    if decision == "deny":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
