"""The restaurant's permanent physical floor layout (not session storage)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PhysicalTable:
    table_id: str
    column: str
    position: int

    @property
    def display_name(self) -> str:
        return f"Table {self.table_id}"


# Row-major order keeps the physical A/B/C/D columns stable in the UI.
PHYSICAL_TABLES = tuple(
    PhysicalTable(f"{column}{position}", column, position)
    for position in range(1, 5)
    for column in "ABCD"
)
