import argparse
import asyncio
import json

from fastmcp import Client


async def invoke(endpoint, token, action, name=None, arguments=None):
    async with Client(endpoint, auth=token) as client:
        if action == "list_projects":
            result = await client.call_tool_mcp("list_projects", {})
        else:
            result = await client.call_tool_mcp(name, arguments or {})
    print(json.dumps(result.model_dump(mode="json", by_alias=True, exclude_none=True), default=str, separators=(",", ":")))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("command", choices=["list_projects", "call"])
    parser.add_argument("name", nargs="?")
    parser.add_argument("--json", dest="arguments", default="{}")
    args = parser.parse_args()
    with open(args.token_file) as file:
        token = file.read().strip()
    arguments = json.loads(args.arguments)
    asyncio.run(invoke(args.endpoint, token, args.command, args.name, arguments))


if __name__ == "__main__":
    main()
