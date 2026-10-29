# venmo-mcp

An MCP server that exposes Venmo functionality to AI assistants like [Claude Code](https://claude.ai/code). Built with the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`MCPServer`) and the unofficial [venmo-api](https://github.com/mmber01/venmo-api) Python library.

## Tools

| Tool | Description |
|------|-------------|
| `search_users` | Search for Venmo users by name or username |
| `send_money` | Send money to a user (supports privacy settings and funding source selection) |
| `request_money` | Request money from a user |
| `get_pending_requests` | List money requests others sent you that are still pending (incomplete) and awaiting your response |
| `complete_request` | Complete (pay) a pending request someone sent you, settling it — **moves real money** |
| `deny_request` | Decline a pending request someone sent you |
| `get_outgoing_requests` | List money requests **you** sent that are still pending (who owes you) |
| `remind_request` | Send a reminder for a pending request you made |
| `cancel_request` | Cancel (withdraw) a pending request you made |
| `split_request` | Request an even split of a total from several users at once (one request each) |
| `lookup_user` | Look up a user's public profile by `user_id` or `username` |
| `get_transactions` | List your recent transactions (newest first); filter by counterparty (`with_user_id`) or note text (`note_contains`), paginate with `before_id` |
| `get_payment_methods` | List available payment methods (balance, bank accounts, cards) |
| `get_friends` | List the authenticated user's friends |
| `get_my_profile` | Get the authenticated user's profile |

## Prerequisites

- [Pixi](https://pixi.sh) (dependency manager)
- A Venmo account

## Setup

1. Clone this repo:

   ```bash
   git clone https://github.com/aaymeloglu/venmo-mcp.git
   cd venmo-mcp
   ```

2. Get your Venmo device ID:

   Venmo requires a real device ID from an active session (random ones are rejected). The easiest way to get one:

   1. Log into [venmo.com](https://venmo.com) in Chrome
   2. Open DevTools (F12) > Network tab
   3. Click around in Venmo (check balance, view a transaction, etc.)
   4. Look for requests to `api.venmo.com` and find the `venmo-device-id` request header
   5. Copy the value (looks like `fp01-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`)

   You only need to do this once. The device ID is saved alongside your token.

3. Authenticate with Venmo:

   ```bash
   pixi run auth
   ```

   This prompts for your device ID, username, and password. Venmo will likely send a 2FA code via SMS -- enter it when prompted. On success, the access token is saved to `.venmo-token`. The token does not expire unless manually revoked.

3. Add to your Claude Code MCP config (`.mcp.json`):

   ```json
   {
     "mcpServers": {
       "venmo": {
         "type": "stdio",
         "command": "pixi",
         "args": [
           "run",
           "--manifest-path",
           "/path/to/venmo-mcp/pixi.toml",
           "python",
           "/path/to/venmo-mcp/server.py"
         ]
       }
     }
   }
   ```

   Replace `/path/to/venmo-mcp` with the actual path to your clone.

4. Optional: to have `send_money` pay from a specific account instead of your Venmo default
   funding source, add an `env` block to the server config with that account's id (from the
   `get_payment_methods` tool):

   ```json
   "env": { "VENMO_FUNDING_SOURCE_ID": "<payment method id>" }
   ```

   A `funding_source_id` passed to `send_money` still takes precedence.

## Running standalone

```bash
pixi run serve
```

## Token management

The access token is stored in `.venmo-token` (gitignored). To re-authenticate, delete the file and run `pixi run auth` again.

## Security

This server can send real money. Use with appropriate caution. The token file is created with `0600` permissions (owner-only read/write).

## TODO

Planned tools and known limitations:

- **Transfer balance to bank (standard / free only).** Cash out the Venmo balance to a linked
  bank account. Must hard-pin the free `type: "standard"` transfer (1–3 business days) and never
  expose the paid instant transfer, so the 1.75% fee can't be triggered. Not reachable with the
  current credentials: cash-out lives on the **web** API (`POST account.venmo.com/api/transfer`),
  which needs a separate web-login session (cookies + CSRF token) and scraping the page's
  `__NEXT_DATA__` JSON for the balance and primary bank id — a different auth path from the mobile
  `api.venmo.com` token this server uses (that token got a `403` against the web host). Reference:
  [radian-software/venmo-auto-transfer](https://github.com/radian-software/venmo-auto-transfer).
  Would also unlock a `get_balance` tool (the balance isn't exposed on the mobile `/v1` API).

- **Venmo Groups request tools** (create / read / add expense / settle group splits). Attempted,
  not currently feasible via the mobile API: `GET /v1/groups` returns an empty list (a legacy
  endpoint), the GraphQL endpoints the app uses return `403`, and no other path variant resolved.
  Would require reverse-engineering the app's Groups endpoints from captured mobile traffic — the
  feature appears to be app-only (not on venmo.com).

## License

MIT
