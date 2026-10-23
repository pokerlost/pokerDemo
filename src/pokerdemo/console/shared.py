"""Small Textual widgets and command parsing shared by both terminal views."""

from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from pokerdemo.protocol.models import Snapshot


class TableView(Static):
    def show(self, snapshot: Snapshot) -> None:
        table = Table(expand=True, box=None, padding=(0, 1))
        for title in ("Player", "Stack", "Bet", "Status", "Link"):
            table.add_column(title)
        for player in snapshot.state.players:
            marker = "> " if player.player_id == snapshot.state.current_player else "  "
            table.add_row(
                Text(marker + player.player_id),
                str(player.stack),
                str(player.street_committed),
                player.status or "waiting",
                "online" if player.connected else "away",
            )
        self.update(table)


def summary(snapshot: Snapshot) -> str:
    state = snapshot.state
    text = (
        f"HAND {state.hand_id or '-'} | {state.phase.upper()} / {state.street or 'LOBBY'}"
        f" | POT {state.pot} | TURN {state.current_player or '-'}\n"
        f"BOARD {' '.join(state.board) or '--'}"
    )
    if snapshot.result:
        payouts = ", ".join(f"{p.player_id} +{p.amount}" for p in snapshot.result.payouts)
        text += f" | RESULT {payouts} | rake {snapshot.result.rake}"
    return text


def command_request(command: str) -> dict[str, object]:
    """Translate human shorthand only; Pydantic and the engine validate the request."""
    words = command.strip().lstrip(":").split()
    if not words:
        raise ValueError("Type start, check, call, fold, bet N, raise N, all_in or settle")
    name, *arguments = words
    name = name.lower()
    if name in {"bet", "raise"} and len(arguments) == 1:
        return {"op": "action", "action": name + "_to", "total": int(arguments[0])}
    if arguments:
        raise ValueError("Unexpected arguments; use bet TOTAL or raise TOTAL")
    if name in {"fold", "check", "call", "all_in"}:
        return {"op": "action", "action": name}
    if name in {"start", "settle", "state", "legal", "result", "leave"}:
        return {"op": name}
    if name in {"next", "n"}:
        return {"op": "start"}
    raise ValueError("Unknown command; type help for available commands")


CSS = """
Screen { background: #111827; color: #e5e7eb; }
Header { height: 1; background: #172c40; }
Footer { height: 1; background: #172c40; }
#summary { height: 2; padding: 0 1; color: #67e8f9; }
TableView { height: auto; max-height: 6; padding: 0 1; }
#details { height: auto; max-height: 3; padding: 0 1; color: #fcd34d; }
#activity { height: 1fr; min-height: 1; padding: 0 1; overflow-y: auto; }
#feedback { height: auto; max-height: 2; padding: 0 1; color: #fca5a5; }
Input { height: 3; margin: 0; border: solid #31566e; }
Input:focus { border: solid #67e8f9; }
"""
