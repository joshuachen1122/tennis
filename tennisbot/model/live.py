"""Turn Kalshi ``live_data`` tennis boards into model score states, and infer each match's format."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from tennisbot.model.markov import MatchFormat, ScoreState

_GAME_POINTS = {0: 0, 15: 1, 30: 2, 40: 3, 50: 4}
LIVE_STATUSES = {"live", "started", "inprogress", "in_progress"}
FINAL_STATUSES = {"closed", "finished", "ended", "complete", "completed"}
SLAM_NAMES = ("us open", "australian open", "french open", "roland", "wimbledon")

# Typical sum of both players' serve-point win rates by tier (hold-rate dominance). Used as the
# prior when backing serve probabilities out of the pre-match price; refined by data later.
SERVE_TOTALS = {
    "KXATPMATCH": 1.28,
    "KXATPCHALLENGERMATCH": 1.25,
    "KXITFMATCH": 1.21,
    "KXWTAMATCH": 1.12,
    "KXWTACHALLENGERMATCH": 1.10,
    "KXITFWMATCH": 1.07,
}


@dataclass
class LiveBoard:
    status: str
    widget_status: str
    sets_1: int
    sets_2: int
    games_1: int
    games_2: int
    raw_points_1: Any
    raw_points_2: Any
    server: str  # competitor id ("" if unknown)
    winner: str
    set_scores: list[tuple[int, int]] = field(default_factory=list)
    delayed: bool = False
    last_set_scoring_type: str = ""
    last_set_tb_points: Optional[int] = None
    match_status: str = ""
    comp1: str = ""
    comp2: str = ""

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES or bool(self.winner)

    def key(self) -> tuple:
        """Identity of the score (changes on every point)."""
        return (self.status, self.sets_1, self.sets_2, self.games_1, self.games_2, self.raw_points_1, self.raw_points_2,
                self.server, self.winner, tuple(self.set_scores))

    def score_text(self) -> str:
        sets = " ".join(f"{a}-{b}" for a, b in self.set_scores)
        return f"{sets} ({self.raw_points_1}-{self.raw_points_2})" if self.is_live else sets


def parse_board(details: dict[str, Any]) -> Optional[LiveBoard]:
    if not details or "status" not in details:
        return None
    r1 = details.get("competitor1_round_scores") or []
    r2 = details.get("competitor2_round_scores") or []
    set_scores = [(int(a.get("score") or 0), int(b.get("score") or 0)) for a, b in zip(r1, r2)]
    ongoing = [i for i, x in enumerate(r1) if x.get("outcome") == "ongoing"]
    games_1 = games_2 = 0
    if ongoing:
        i = ongoing[-1]
        games_1, games_2 = set_scores[i]
    sets_1 = sum(1 for x in r1 if x.get("outcome") == "winner")
    sets_2 = sum(1 for x in r2 if x.get("outcome") == "winner")
    tbp = details.get("last_set_tiebreak_points_to_win")
    return LiveBoard(
        status=str(details.get("status") or ""),
        widget_status=str(details.get("widget_status") or ""),
        sets_1=sets_1,
        sets_2=sets_2,
        games_1=games_1,
        games_2=games_2,
        raw_points_1=details.get("competitor1_current_round_score"),
        raw_points_2=details.get("competitor2_current_round_score"),
        server=str(details.get("server") or ""),
        winner=str(details.get("winner") or ""),
        set_scores=set_scores,
        delayed=bool(details.get("live_data_delayed")),
        last_set_scoring_type=str(details.get("last_set_scoring_type") or ""),
        last_set_tb_points=int(tbp) if tbp else None,
        match_status=str(details.get("match_status") or ""),
        comp1=str(details.get("competitor1_id") or ""),
        comp2=str(details.get("competitor2_id") or ""),
    )


def infer_format(tournament: str, best_of: Any, is_doubles: bool, board: Optional[LiveBoard] = None) -> Optional[MatchFormat]:
    """None means a format the model does not support (e.g. Next Gen Finals short sets)."""
    name = (tournament or "").lower()
    if "next gen" in name:
        return None
    try:
        bo = int(best_of) if best_of else 3
    except (TypeError, ValueError):
        bo = 3
    if bo not in (3, 5):
        return None
    final = "tb7"
    if board and board.last_set_scoring_type.lower() == "matchtiebreak":
        final = "mtb10"
    elif (board and board.last_set_tb_points == 10) or any(s in name for s in SLAM_NAMES):
        final = "tb10"
    elif is_doubles:
        final = "mtb10"
    return MatchFormat(best_of=bo, final_set=final, no_ad=is_doubles and "wimbledon" not in name)  # type: ignore[arg-type]


def to_state(board: LiveBoard, fmt: MatchFormat, comp_a: str, comp_b: str) -> Optional[ScoreState]:
    """Score state from player A's perspective, where A is competitor 1 on the board. Returns None
    when the board is not tradeable (unknown server, unparseable points)."""
    if not board.server or board.server not in (comp_a, comp_b):
        return None
    a_serving = board.server == comp_a
    in_tb = (board.games_1 == 6 and board.games_2 == 6) or (
        board.sets_1 == fmt.sets_to_win - 1 and board.sets_2 == fmt.sets_to_win - 1 and fmt.final_set == "mtb10"
    )
    try:
        p1, p2 = int(board.raw_points_1 or 0), int(board.raw_points_2 or 0)
    except (TypeError, ValueError):
        return None
    if not in_tb:
        if p1 not in _GAME_POINTS or p2 not in _GAME_POINTS:
            return None
        p1, p2 = _GAME_POINTS[p1], _GAME_POINTS[p2]
        if p1 == 4 and p2 < 3:  # some feeds send 50 only as AD; anything else is malformed
            return None
    return ScoreState(board.sets_1, board.sets_2, board.games_1, board.games_2, p1, p2, a_serving)


def point_winner(prev: LiveBoard, cur: LiveBoard) -> Optional[int]:
    """1 or 2 if exactly one point separates two consecutive boards (so serve stats can be
    attributed), else None."""
    if prev.sets_1 + prev.sets_2 != cur.sets_1 + cur.sets_2:
        # set ended on this point: the set winner won it (only if the previous board was set point)
        if cur.sets_1 > prev.sets_1:
            return 1
        if cur.sets_2 > prev.sets_2:
            return 2
        return None
    dg1, dg2 = cur.games_1 - prev.games_1, cur.games_2 - prev.games_2
    if (dg1, dg2) == (1, 0):
        return 1
    if (dg1, dg2) == (0, 1):
        return 2
    if (dg1, dg2) != (0, 0):
        return None
    try:
        a0, b0, a1, b1 = (int(x or 0) for x in (prev.raw_points_1, prev.raw_points_2, cur.raw_points_1, cur.raw_points_2))
    except (TypeError, ValueError):
        return None
    tb = prev.games_1 == 6 and prev.games_2 == 6
    if tb:
        if (a1 - a0, b1 - b0) == (1, 0):
            return 1
        if (a1 - a0, b1 - b0) == (0, 1):
            return 2
        return None
    order = [0, 15, 30, 40, 50]
    if a0 not in order or b0 not in order or a1 not in order or b1 not in order:
        return None
    ia0, ib0, ia1, ib1 = order.index(a0), order.index(b0), order.index(a1), order.index(b1)
    if (ia1 - ia0, ib1 - ib0) == (1, 0) or (a0 == 40 and b0 == 50 and a1 == 40 and b1 == 40):
        return 1
    if (ia1 - ia0, ib1 - ib0) == (0, 1) or (a0 == 50 and b0 == 40 and a1 == 40 and b1 == 40):
        return 2
    return None
