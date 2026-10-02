import asyncio
import logging
import sys

from CommonClient import CommonContext, ClientCommandProcessor, server_loop, get_base_parser, handle_url_arg
from MultiServer import mark_raw
from Utils import gui_enabled

class HintCommandProcessor(ClientCommandProcessor):
    @mark_raw
    def _cmd_list_hints(self, slot_name: str = "") -> None:
        """List every ArchiLogic hint pointing at a player's world. Usage: /hints <slot name>"""
        hints = getattr(self.ctx, "archilogic_hints", None)
        if not hints:
            self.output("No ArchiLogic hints loaded — is this the ArchiLogic slot?")
            return

        by_name = {name.lower(): slot for slot, name in self.ctx.player_names.items()}
        slot = by_name.get(slot_name.strip().lower())
        if slot is None:
            known = ", ".join(sorted(n for s, n in self.ctx.player_names.items() if s))
            self.output(f"Unknown slot name. Known slots: {known}")
            return

        tagged = [hint for hint in hints if hint.get("finder_id") == slot]
        if not tagged:
            self.output(f"No hints point at {self.ctx.player_names[slot]}'s world.")
            return

        self.output(f"{len(tagged)} hints pointing at {self.ctx.player_names[slot]}'s world:")
        for hint in tagged:
            self.output(f"  [Hint for {slot_name}] {hint['hint_text']}")


class HintContext(CommonContext):
    tags = CommonContext.tags | {"TextOnly"}
    game = ""
    items_handling = 0b111
    # ArchiLogic stores its hints on slot_data
    want_slot_data = True
    command_processor = HintCommandProcessor

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect(game="")

    def on_package(self, cmd: str, args: dict):
        if cmd == "Connected":
            self.game = self.slot_info[self.slot].game
            self.archilogic_hints = args.get("slot_data", {}).get("hints", [])
            logging.info("ArchiLogic: received %d hints.", len(self.archilogic_hints))

    async def disconnect(self, allow_autoreconnect: bool = False):
        self.game = ""
        self.archilogic_hints = []
        await super().disconnect(allow_autoreconnect)


def run_client(*args):
    async def main(parsed):
        ctx = HintContext(parsed.connect, parsed.password)
        ctx.auth = parsed.name
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")

        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()

        await ctx.exit_event.wait()
        await ctx.shutdown()

    import colorama

    parser = get_base_parser(description="ArchiLogic hint client.")
    parser.add_argument("--name", default=None, help="Slot Name to connect as.")
    parser.add_argument("url", nargs="?", help="Archipelago connection url")
    parsed = parser.parse_args(args)
    parsed = handle_url_arg(parsed, parser=parser)

    colorama.just_fix_windows_console()
    asyncio.run(main(parsed))
    colorama.deinit()


if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    run_client(*sys.argv[1:])
