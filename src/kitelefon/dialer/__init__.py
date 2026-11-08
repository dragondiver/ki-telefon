from typing import Protocol


class Dialer(Protocol):
    # Freisprechen (#38): darf der CallManager waehrend eines Gespraechs in
    # einem Begleit-Task weiterlesen (Ziffer 1 = Freisprechen umschalten)?
    # Voraussetzung ist, dass read_digit() Timeout und Abbruch unbeschadet
    # uebersteht. GpioDialer und KeyboardDialer (seit #74) lesen dafuer
    # beide aus einer Queue.
    im_gespraech_lesbar: bool

    async def read_digit(self, timeout: float) -> int | None: ...

    def leeren(self) -> None:
        """Liegengebliebene Ziffern verwerfen (Waehlgeraeusch vor dem Gespraech)."""
        ...


def make_dialer(platform: str) -> Dialer:
    if platform == "mac":
        from kitelefon.dialer.keyboard import KeyboardDialer

        return KeyboardDialer()
    if platform == "pi":
        from kitelefon.dialer.gpio import GpioDialer

        return GpioDialer()
    raise ValueError(f"unknown platform: {platform!r}")
