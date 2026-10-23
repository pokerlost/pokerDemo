"""A small amount picker using the engine's current inclusive legal range."""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.validation import Integer
from textual.widgets import Button, Input, Static

from pokerdemo.protocol.models import AmountRange


class WagerDialog(ModalScreen[tuple[str, int, int] | None]):
    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(
        self, action: str, bounds: AmountRange, version: int, pot: int, committed: int, call: int
    ) -> None:
        super().__init__()
        self.action_name, self.bounds, self.version = action, bounds, version
        base = committed + call
        self.presets = {
            "minimum": bounds.minimum,
            "half-pot": min(bounds.maximum, max(bounds.minimum, base + (pot + call) // 2)),
            "pot": min(bounds.maximum, max(bounds.minimum, base + pot + call)),
        }

    def compose(self) -> ComposeResult:
        with Vertical(id="wager-dialog"):
            yield Static(f"{self.action_name.upper()} TO", id="wager-title")
            yield Static(
                f"{self.bounds.minimum:,} to {self.bounds.maximum:,} chips\n"
                "Street total, including chips already committed.",
                id="wager-range",
            )
            yield Input(
                str(self.bounds.minimum),
                type="integer",
                id="amount",
                max_length=12,
                validators=Integer(minimum=self.bounds.minimum, maximum=self.bounds.maximum),
                valid_empty=False,
            )
            with Horizontal(id="presets"):
                for key, label in (("minimum", "Min"), ("half-pot", "½ pot"), ("pot", "Pot")):
                    yield Button(f"{label} {self.presets[key]:,}", id=key)
            yield Static("Choose an amount, then confirm.", id="amount-message", markup=False)
            with Horizontal(id="wager-confirmation"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", id="confirm", variant="success")

    def on_mount(self) -> None:
        self.set_focus(self.query_one("#amount", Input))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def confirm(self) -> None:
        field = self.query_one("#amount", Input)
        result = field.validate(field.value)
        if result is None or not result.is_valid:
            self.query_one("#amount-message", Static).update(
                f"Enter a whole number from {self.bounds.minimum:,} to {self.bounds.maximum:,}."
            )
            field.focus()
            return
        self.dismiss((self.action_name, int(field.value), self.version))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.confirm()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        key = event.button.id
        if key in self.presets:
            field = self.query_one("#amount", Input)
            field.value = str(self.presets[key])
            field.focus()
        elif key == "confirm":
            self.confirm()
        elif key == "cancel":
            self.action_cancel()
