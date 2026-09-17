import asyncio
import logging
import sys

from CommonClient import CommonContext, server_loop, get_base_parser, handle_url_arg
from Utils import gui_enabled


def run_client(*args):
    class HintContext(CommonContext):
        # Text Mode to use !hint and such with games that have no text entry
        tags = CommonContext.tags | {"TextOnly"}
        game = ""  # empty matches any game since 0.3.2
        items_handling = 0b111  # receive all items for /received
        want_slot_data = True  # Can't use game specific slot_data

        async def server_auth(self, password_requested: bool = False):
            if password_requested and not self.password:
                await super(HintContext, self).server_auth(password_requested)
            await self.get_username()
            await self.send_connect(game="")

        def on_package(self, cmd: str, args: dict):
            if cmd == "Connected":
                self.game = self.slot_info[self.slot].game

        async def disconnect(self, allow_autoreconnect: bool = False):
            self.game = ""
            await super().disconnect(allow_autoreconnect)

    async def main(args):
        ctx = HintContext(args.connect, args.password)
        ctx.auth = args.name
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")

        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()

        await ctx.exit_event.wait()
        await ctx.shutdown()

    import colorama

    parser = get_base_parser(description="Gameless Archipelago Client, for text interfacing.")
    parser.add_argument('--name', default=None, help="Slot Name to connect as.")
    parser.add_argument("url", nargs="?", help="Archipelago connection url")
    args = parser.parse_args(args)

    args = handle_url_arg(args, parser=parser)

    # use colorama to display colored text highlighting on windows
    colorama.just_fix_windows_console()

    asyncio.run(main(args))
    colorama.deinit()



if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)  # force log-level to work around log level resetting to WARNING
    run_client(*sys.argv[1:])  # default value for parse_args
