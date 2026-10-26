from ._profile import GameProfile

class Jigsaw(GameProfile):
    game = "Jigsaw"

    prefer_loc_over_region = True

    display_names = {
        "1 Puzzle Piece": "Puzzle Piece",
    }