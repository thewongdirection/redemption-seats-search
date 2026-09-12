# Using a seats.aero MCP server instead of the script

seats.aero publishes no official MCP server. The most complete community option is
[gavgrego/seats.aero-mcp-server](https://github.com/gavgrego/seats.aero-mcp-server) (MPL-2.0, TypeScript,
Node 20+). It wraps the same Partner API endpoints the script uses.

## Install

```bash
git clone https://github.com/gavgrego/seats.aero-mcp-server ~/tools/seats-mcp
cd ~/tools/seats-mcp && pnpm install && pnpm build
```

## Configure Claude Code

Add to `~/.claude.json` (user scope) or `.mcp.json` in a project. Reference the key through an environment
variable so it never lands in a committed file:

```json
{
  "mcpServers": {
    "seats": {
      "command": "node",
      "args": ["/absolute/path/to/seats-mcp/build/index.js"],
      "env": { "SEATS_API_KEY": "${SEATS_AERO_API_KEY}" }
    }
  }
}
```

## Tool mapping

| Skill step | MCP tool | Arguments |
|---|---|---|
| Cached search | `get_flights` | `originAirport`, `destinationAirport`, `startDate`, `endDate`, `cabins: "business,first"`, `take: 500` |
| Flight detail | `get_trips` | `id: <Availability.ID>` |
| Not available to Pro keys | `live_search` | needs a commercial agreement |

Apply the skill's filtering rules yourself when using the MCP tools: keep only `JAvailable`/`FAvailable`
records, keep only trips whose `Cabin` is `business` or `first`, drop `Filtered: true`, drop trips whose
`RemainingSeats` is between 1 and (party size − 1), treat `0` as unknown, and sort by `MileageCost`.
