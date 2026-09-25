"""Deterministic in-game clock owned by the server, never by the model."""


class GameClock:
    """Day/minute counter formatted as 'Day X HH:MM'."""

    MINUTES_PER_DAY = 24 * 60

    def __init__(self, day: int = 1, minute: int = 390) -> None:
        """Start at the given day and minute-of-day (default Day 1 06:30)."""
        self.day = day
        self.minute = minute

    def add_minutes(self, minutes: int) -> None:
        """Advance the clock by a validated non-negative number of minutes."""
        if minutes < 0:
            raise ValueError("Elapsed minutes cannot be negative.")
        total = self.day * self.MINUTES_PER_DAY + self.minute + minutes
        self.day, self.minute = divmod(total, self.MINUTES_PER_DAY)

    def format(self) -> str:
        """Return the display label, e.g. 'Day 1 06:30'."""
        hours, minutes = divmod(self.minute, 60)
        return f"Day {self.day} {hours:02d}:{minutes:02d}"

    @classmethod
    def label_from_total(cls, total_minutes: int) -> str:
        """Return a display label for an absolute minute count."""
        day, minute = divmod(total_minutes, cls.MINUTES_PER_DAY)
        return cls(day=day, minute=minute).format()

    def to_dict(self) -> dict[str, int]:
        """Serialize the clock for room persistence."""
        return {"day": self.day, "minute": self.minute}

    @classmethod
    def from_dict(cls, data: object) -> "GameClock":
        """Rebuild a clock from persisted data, tolerating old saves."""
        if isinstance(data, dict):
            day = int(data.get("day", 1))
            minute = int(data.get("minute", 390))
            return cls(day=day, minute=minute)
        return cls()
