"""Release-independent safety gates backed by current real-client evidence."""

from __future__ import annotations

# Linux WeChat 4.1.1.8 exposes self and peer messages as indistinguishable
# AT-SPI list items. Keep enrollment and connector startup closed until a later
# implementation has a separately reviewed proof of direction.
AT_SPI_DIRECTION_GATE_OPEN = False
AT_SPI_DIRECTION_GATE_REASON = "atspi_direction_unproven"
